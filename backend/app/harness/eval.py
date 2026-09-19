"""Small, fixed offline regression set for harness behavior; not an LLM benchmark."""

import asyncio
import json
import re
import tempfile
from pathlib import Path

from backend.app.harness.runtime import Harness
from backend.app.harness.store import RunStore


CASES = Path(__file__).resolve().parents[3] / "evals" / "harness_cases.json"


def check_case(run: dict, case: dict) -> dict[str, bool]:
    """Check the saved trace against the expected tools and their actual outputs."""
    answer = run["answer"] or ""
    calls = [item for item in run["transcript"] if item.get("type") == "function_call"]
    outputs = [item for item in run["transcript"] if item.get("type") == "function_call_output"]
    call_ids = [item.get("call_id") for item in calls]
    output_ids = [item.get("call_id") for item in outputs]
    results = {}
    try:
        for output in outputs:
            results[output["call_id"]] = json.loads(output["output"])
    except (KeyError, TypeError, json.JSONDecodeError):
        results = {}
    tool_results = (len(call_ids) == len(output_ids) == len(results)
                    and set(call_ids) == set(output_ids)
                    and all(isinstance(result, dict) and result.get("ok") is True
                            for result in results.values()))

    if case["required_tools"]:
        by_name = {call["name"]: results.get(call.get("call_id"), {}).get("data", {})
                   for call in calls}
        metrics = by_name.get("payment_metrics", {})
        documents = by_name.get("search_incident_docs", {}).get("matches", [])
        if tool_results and metrics and documents:
            versions = metrics.get("versions", [])
            version = next((item for item in versions if item.get("version") == "3.2.1"), None)
            expected_counts = [metrics.get("previous_count"), metrics.get("current_count"),
                               version.get("count") if version else None]
            allowed_counts = {str(value) for value in expected_counts}
            allowed_counts.update(str(item["count"]) for item in versions if "count" in item)
            mentioned_counts = set(re.findall(r"(?<![\d.])(\d+)\s*条", answer))
            evidence_match = (all(value is not None and f"{value} 条" in answer
                                  for value in expected_counts)
                              and mentioned_counts <= allowed_counts
                              and f"{metrics.get('growth_rate_percent')}%" in answer
                              and "3.2.1" in answer
                              and any(doc.get("source") in answer for doc in documents
                                      if doc.get("source")))
        else:
            evidence_match = False
    else:
        evidence_match = not calls and "无法凭现有证据判断" in answer

    return {"completed": run["status"] == "completed",
            "tool_selection": [call.get("name") for call in calls] == case["required_tools"],
            "tool_results": tool_results,
            "answer": all(phrase in answer for phrase in case["required_phrases"]),
            "evidence_match": evidence_match}


async def evaluate() -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory() as temp:
        harness = Harness(RunStore(Path(temp) / "eval.db"))
        for case in cases:
            run = await harness.start(case["question"])
            checks = check_case(run, case)
            results.append({"id": case["id"], "passed": all(checks.values()), "checks": checks})
    return {"total": len(results), "passed": sum(row["passed"] for row in results), "cases": results,
            "mode": "demo"}


if __name__ == "__main__":
    report = asyncio.run(evaluate())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["total"] else 1)
