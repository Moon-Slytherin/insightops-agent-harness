"""Allowlisted read-only tools exposed as strict Responses function schemas."""

import json
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.app.analytics import payment_incident_metrics
from backend.app.database import initialize_database
from backend.app.retriever import search_knowledge


class MetricsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(description="Must be 支付失败")


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], dict]

    def spec(self) -> dict:
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": self.parameters, "strict": True}


def metrics(args: dict) -> dict:
    parsed = MetricsArgs.model_validate(args)
    if parsed.category != "支付失败":
        raise ValueError("此演示只包含支付失败事故的固定统计窗口")
    initialize_database()
    return {"source": "data/feedback.csv", "period": "2026-08-03..2026-08-09",
            **payment_incident_metrics()}


def knowledge(args: dict) -> dict:
    parsed = SearchArgs.model_validate(args)
    return {"matches": search_knowledge(parsed.query)}


TOOLS = {
    item.name: item for item in (
        Tool("payment_metrics", "Read payment incident counts and version distribution from the local data.",
             {"type": "object", "properties": {"category": {"type": "string", "description": "支付失败"}},
              "required": ["category"], "additionalProperties": False}, metrics),
        Tool("search_incident_docs", "Search local incident documents for evidence with source paths.",
             {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}, knowledge),
    )
}


def execute(name: str, arguments: str, max_output_chars: int = 5000) -> str:
    """Return a bounded JSON result; never execute model-supplied SQL or shell."""
    if name not in TOOLS:
        return json.dumps({"error": "unknown_tool", "tool": name}, ensure_ascii=False)
    try:
        if len(arguments) > 2000:
            raise ValueError("arguments_too_long")
        value = TOOLS[name].handler(json.loads(arguments))
        result = json.dumps({"ok": True, "data": value}, ensure_ascii=False)
        if len(result) > max_output_chars:
            raise ValueError("tool_output_too_long")
        return result
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)[:400]}, ensure_ascii=False)
