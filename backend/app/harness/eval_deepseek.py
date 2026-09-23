"""Sequential, paid DeepSeek evaluation with inspectable per-case evidence.

This is a project-specific regression set, not a public benchmark. It makes real
API calls and therefore requires an explicit key and may incur a small charge.
"""

import argparse
import asyncio
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backend.app.harness.grounding import validate_answer
from backend.app.harness.runtime import Harness
from backend.app.harness.store import RunStore


ROOT = Path(__file__).resolve().parents[3]
CASES = ROOT / "evals" / "deepseek_cases.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "deepseek_latest.json"
ALLOWED_TOOLS = {"payment_metrics", "search_incident_docs"}


def _has(pattern: str, answer: str) -> bool:
    return re.search(pattern, answer, re.IGNORECASE) is not None


FACT_CHECKS = {
    "current_count": lambda text: _has(
        r"(?:本期|当期|当前|本次周期)[^。\n]{0,35}(?:\|\s*)?\*{0,2}18\*{0,2}(?:\s*\||\s*(?:条|起|次)|(?=\s*[；;，,]))"
        r"|18\s*(?:条|起|次)[^。\n]{0,25}(?:本期|当期|当前|本次周期)", text),
    "previous_count": lambda text: _has(
        r"(?:上期|上一期|上一周期|前一期|上周|前一周)[^。\n]{0,35}(?:\|\s*)?\*{0,2}5\*{0,2}(?:\s*\||\s*(?:条|起|次)|(?=\s*[；;，,]))"
        r"|5\s*(?:条|起|次)[^。\n]{0,25}(?:上期|上一期|上一周期|前一期|上周|前一周)", text),
    "growth_rate": lambda text: _has(r"(?:260(?:\.0)?\s*%|增长[^。\n]{0,12}260)", text),
    "version_3_2_1": lambda text: _has(
        r"3\.2\.1[^。\n]{0,30}(?:=|：|:|占|有|计)?\s*\*{0,2}16\*{0,2}(?:\s*\||\s*(?:条|起|次)|(?=\s*[；;，,]))"
        r"|16\s*(?:条|起|次)[^。\n]{0,25}3\.2\.1", text),
    "version_3_2_0": lambda text: _has(
        r"3\.2\.0[^。\n]{0,30}(?:=|：|:|占|有|计)?\s*\*{0,2}2\*{0,2}(?:\s*\||\s*(?:条|起|次)|(?=\s*[；;，,]))"
        r"|2\s*(?:条|起|次)[^。\n]{0,25}3\.2\.0", text),
    "sdk_or_retry": lambda text: "SDK" in text.upper() or "重试" in text,
    "playbook_action": lambda text: any(word in text for word in ("错误日志", "错误码", "平台", "重试配置", "SDK")),
}


def _policy_ok(policy: str, answer: str) -> bool:
    insufficient = _has(
        r"(?:无法|不能|不足以|尚不能|缺少|没有|未提供|不包含|不在.{0,12}范围|现有.{0,8}(?:不足|无法|不能))",
        answer,
    )
    causal = _has(
        r"(?:不能|无法|不足以|尚不能)[^。\n]{0,30}(?:因果|根因|证明)"
        r"|(?:因果|根因)[^。\n]{0,15}(?:尚不能|无法|不能|未能)[^。\n]{0,10}(?:确认|证明|判断)?"
        r"|相关[^。\n]{0,20}(?:不等于|并非)[^。\n]{0,12}因果|不构成[^。\n]{0,12}因果",
        answer,
    )
    if policy == "factual":
        return True
    if policy == "causal_caution":
        return causal
    if policy in {"unsupported", "insufficient_evidence"}:
        return insufficient
    if policy == "correct_false_premise":
        return insufficient or _has(r"(?:实际|数据显示|工具结果|现有数据)[^。\n]{0,25}18\s*条", answer)
    raise ValueError(f"unknown policy: {policy}")


def _tool_trace(run: dict) -> tuple[list[dict], dict[str, dict]]:
    calls = [item for item in run.get("transcript", []) if item.get("type") == "function_call"]
    outputs = {}
    for item in run.get("transcript", []):
        if item.get("type") != "function_call_output":
            continue
        try:
            outputs[item.get("call_id")] = json.loads(item.get("output", ""))
        except (TypeError, json.JSONDecodeError):
            outputs[item.get("call_id")] = {"ok": False, "error": "invalid_json"}
    return calls, outputs


def _performance(events: list[dict]) -> dict:
    model_events = [event for event in events if event.get("kind") == "model_response"]
    usages = [event.get("detail", {}).get("usage") or {} for event in model_events]
    return {
        "model_latency_ms": round(sum(event.get("detail", {}).get("latency_ms", 0)
                                      for event in model_events), 2),
        "prompt_tokens": sum(usage.get("prompt_tokens", usage.get("input_tokens", 0))
                             for usage in usages),
        "completion_tokens": sum(usage.get("completion_tokens", usage.get("output_tokens", 0))
                                 for usage in usages),
        "total_tokens": sum(usage.get("total_tokens", 0) for usage in usages),
    }


def score_case(run: dict, case: dict, events: list[dict] | None = None) -> dict:
    """Score observable behavior without asking another model to be the judge."""
    answer = run.get("answer") or ""
    calls, outputs = _tool_trace(run)
    names = [item.get("name") for item in calls]
    expected = case["expected_tools"]
    match = case.get("tool_match", "exact")
    if match == "contains":
        tool_selection = set(expected).issubset(names) and set(names).issubset(ALLOWED_TOOLS)
    elif match == "allowed":
        tool_selection = (set(expected).issubset(names)
                          and set(names).issubset(case.get("allowed_tools", expected)))
    else:
        tool_selection = sorted(names) == sorted(expected)
    tool_execution = (
        len(calls) == len(outputs)
        and all(item.get("call_id") in outputs for item in calls)
        and all(outputs[item.get("call_id")].get("ok") is True for item in calls)
    )
    facts = {name: FACT_CHECKS[name](answer) for name in case.get("required_facts", [])}
    forbidden = {phrase: phrase not in answer for phrase in case.get("forbidden_phrases", [])}
    checks = {
        "completed": run.get("status") == "completed",
        "tool_selection": tool_selection,
        "tool_execution": tool_execution,
        "required_facts": all(facts.values()),
        "policy": _policy_ok(case["policy"], answer),
        "numeric_consistency": not validate_answer(answer),
        "forbidden_phrases": all(forbidden.values()),
    }
    return {
        "id": case["id"],
        "question": case["question"],
        "passed": all(checks.values()),
        "checks": checks,
        "fact_checks": facts,
        "forbidden_checks": forbidden,
        "status": run.get("status"),
        "tools": names,
        "rounds": run.get("rounds"),
        "tool_calls": run.get("tool_calls"),
        "answer": answer,
        "error": run.get("error"),
        "performance": _performance(events or []),
    }


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    check_names = list(rows[0]["checks"]) if rows else []
    metrics = {
        name: {"passed": sum(row["checks"][name] for row in rows), "total": total,
               "rate": round(sum(row["checks"][name] for row in rows) / total, 4) if total else 0.0}
        for name in check_names
    }
    passed = sum(row["passed"] for row in rows)
    total_latency = round(sum(row.get("performance", {}).get("model_latency_ms", 0)
                              for row in rows), 2)
    total_tokens = sum(row.get("performance", {}).get("total_tokens", 0) for row in rows)
    return {"total": total, "passed": passed,
            "case_pass_rate": round(passed / total, 4) if total else 0.0,
            "metrics": metrics,
            "performance": {
                "total_model_latency_ms": total_latency,
                "avg_model_latency_ms_per_case": round(total_latency / total, 2) if total else 0.0,
                "total_tokens": total_tokens,
                "avg_tokens_per_case": round(total_tokens / total, 2) if total else 0.0,
            },
            "failed_case_ids": [row["id"] for row in rows if not row["passed"]]}


def rescore_report(report: dict, cases: list[dict]) -> dict:
    """Re-apply current deterministic rules to a saved report without API calls."""
    by_id = {case["id"]: case for case in cases}
    rows = []
    for old in report.get("cases", []):
        if old.get("id") not in by_id:
            raise ValueError(f"saved report has unknown case: {old.get('id')}")
        case = by_id[old["id"]]
        names = old.get("tools", [])
        expected = case["expected_tools"]
        match = case.get("tool_match", "exact")
        if match == "contains":
            tool_selection = set(expected).issubset(names) and set(names).issubset(ALLOWED_TOOLS)
        elif match == "allowed":
            tool_selection = (set(expected).issubset(names)
                              and set(names).issubset(case.get("allowed_tools", expected)))
        else:
            tool_selection = sorted(names) == sorted(expected)
        answer = old.get("answer") or ""
        facts = {name: FACT_CHECKS[name](answer) for name in case.get("required_facts", [])}
        forbidden = {phrase: phrase not in answer for phrase in case.get("forbidden_phrases", [])}
        checks = {
            "completed": old.get("status") == "completed",
            # A saved summary does not contain raw tool outputs. Preserve the
            # original execution check while re-scoring answer/routing rules.
            "tool_selection": tool_selection,
            "tool_execution": bool(old.get("checks", {}).get("tool_execution")),
            "required_facts": all(facts.values()),
            "policy": _policy_ok(case["policy"], answer),
            "numeric_consistency": not validate_answer(answer),
            "forbidden_phrases": all(forbidden.values()),
        }
        row = dict(old)
        row.update({"passed": all(checks.values()), "checks": checks,
                    "fact_checks": facts, "forbidden_checks": forbidden})
        rows.append(row)
    rescored = dict(report)
    rescored["schema_version"] = 2
    rescored["rescored_at"] = datetime.now(timezone.utc).isoformat()
    rescored["rescored_from_generated_at"] = report.get("generated_at")
    rescored["summary"] = summarize(rows)
    rescored["cases"] = rows
    return rescored


async def evaluate(cases: list[dict], output: Path, model: str | None = None) -> dict:
    rows = []
    with tempfile.TemporaryDirectory() as temp:
        if model:
            from backend.app.harness.model import DeepSeekResponses
            harness = Harness(RunStore(Path(temp) / "deepseek_eval.db"),
                              lambda mode: DeepSeekResponses(model=model))
        else:
            harness = Harness(RunStore(Path(temp) / "deepseek_eval.db"))
        for index, case in enumerate(cases, 1):
            run = await harness.start(case["question"], mode="deepseek")
            row = score_case(run, case, harness.store.events(run["run_id"]))
            rows.append(row)
            print(f"[{index}/{len(cases)}] {case['id']}: "
                  f"{'PASS' if row['passed'] else 'FAIL'} "
                  f"status={row['status']} tools={row['tools']}", flush=True)
    report = {
        "schema_version": 1,
        "mode": "deepseek",
        "model": model or "DEEPSEEK_MODEL/default",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "project-authored payment-incident regression set; not a public benchmark",
        "summary": summarize(rows),
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paid, sequential DeepSeek regression cases")
    parser.add_argument("--limit", type=int, help="run only the first N cases (recommended first: 3)")
    parser.add_argument("--case", action="append", dest="case_ids", help="run one case id; repeatable")
    parser.add_argument("--model", help="override DEEPSEEK_MODEL for this evaluation")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--rescore", type=Path,
                        help="re-score an existing JSON report without calling the API")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.rescore:
        if args.case_ids or args.limit is not None or args.model:
            raise SystemExit("--rescore cannot be combined with --case, --limit or --model")
        saved = json.loads(args.rescore.read_text(encoding="utf-8"))
        report = rescore_report(saved, cases)
        output = (args.rescore.with_name(args.rescore.stem + "_rescored.json")
                  if args.output == DEFAULT_OUTPUT else args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"report={output}")
        return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            raise SystemExit("unknown case id(s): " + ", ".join(sorted(missing)))
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        cases = cases[:args.limit]
    try:
        report = asyncio.run(evaluate(cases, args.output, args.model))
    except ValueError as exc:
        if "DEEPSEEK_API_KEY" in str(exc):
            raise SystemExit(
                "未检测到完整的 DEEPSEEK_API_KEY。请只在当前 PowerShell 环境变量中设置，"
                "不要写入代码或提交到 Git。"
            ) from exc
        raise
    summary = report["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={args.output}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
