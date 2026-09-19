"""轻量知识库检索工具。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT / "data" / "knowledge"


def search_knowledge(question: str) -> list[dict]:
    keywords = {"支付", "失败", "3.2.1", "重试", "sdk", "版本"}
    query_terms = {term for term in keywords if term.lower() in question.lower()}
    matches: list[dict] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        body = path.read_text(encoding="utf-8")
        score = sum(term.lower() in body.lower() for term in query_terms)
        if score:
            useful_lines = [
                line.strip("- ")
                for line in body.splitlines()
                if any(term.lower() in line.lower() for term in query_terms) and not line.startswith("#")
            ]
            matches.append(
                {
                    "title": path.stem.replace("_", " "),
                    "source": f"data/knowledge/{path.name}",
                    "snippet": "；".join(useful_lines[:2]),
                    "score": score,
                }
            )
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:2]
