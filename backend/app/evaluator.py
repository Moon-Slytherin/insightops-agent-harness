"""V1 回归评测器：检查工具、证据和答案关键词。"""

import json
from pathlib import Path

from backend.app.agent import investigate
from backend.app.schemas import EvalCaseResult, EvalReport


ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "evals" / "cases.json"


def run_evaluation() -> EvalReport:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results: list[EvalCaseResult] = []
    for case in cases:
        response = investigate(case["question"])
        tools = {step.tool for step in response.trace if step.status == "success"}
        checks = {
            "behavior": bool(response.evidence) == case["expects_evidence"],
            "required_terms": all(term in response.answer for term in case["required_terms"]),
            "required_tools": set(case["required_tools"]).issubset(tools),
        }
        results.append(EvalCaseResult(id=case["id"], question=case["question"], passed=all(checks.values()), checks=checks))
    passed = sum(result.passed for result in results)
    return EvalReport(total=len(results), passed=passed, pass_rate_percent=round(passed / len(results) * 100, 1), cases=results)
