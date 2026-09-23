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
        Tool("payment_metrics", (
             "仅返回固定统计窗口内的支付失败投诉总数、上期总数、增长率，以及 "
             "3.2.1/3.2.0 版本分布。仅在问题需要这些数字时调用。"
             "不包含退款到账、会员开通、Android/iOS 平台拆分、服务端错误码、安装量、"
             "支付尝试次数或真实失败率。"),
             {"type": "object", "properties": {"category": {"type": "string", "description": "支付失败"}},
              "required": ["category"], "additionalProperties": False}, metrics),
        Tool("search_incident_docs", (
             "仅搜索本地的 3.2.1 发布说明和支付事故排查手册，并返回来源路径。"
             "仅在问题需要版本变更、排查步骤或事故文档证据时调用。"
             "不包含退款到账、会员开通、源代码、服务端日志/错误码、安装量或平台统计。"),
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
