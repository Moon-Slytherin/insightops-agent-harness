"""Deterministic checks for contradictions inside a model's final answer."""

import re


def validate_answer(answer: str) -> list[str]:
    """Return human-readable issues that should be corrected before completion."""
    issues = []
    # Examples: "13 条投诉里，16 条来自新版" or "10 条中有 12 条".
    subset_claims = re.findall(
        r"(\d+)\s*条[^。；\n]{0,30}?(?:中|里)[^。；\n]{0,15}?(\d+)\s*条",
        answer,
    )
    for total_text, subset_text in subset_claims:
        total, subset = int(total_text), int(subset_text)
        if subset > total:
            issues.append(
                f"数量关系矛盾：子集 {subset} 条不能大于所述总数 {total} 条"
            )
    return issues
