"""Responses API adapter and explicit, offline scripted model for repeatable demos."""

import json
import os
from typing import Protocol

import httpx


SYSTEM = ("你是 InsightOps 事故调查助手。先调用工具获取证据；回答要区分相关性与因果性。"
          "若没有相关证据，明确说明无法判断，不编造。仅可使用本地给定工具。")


class Model(Protocol):
    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]: ...


class OpenAIResponses:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for openai mode")

    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "instructions": SYSTEM, "input": transcript,
                      "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False,
                      "store": False},
            )
            response.raise_for_status()
            body = response.json()
        if body.get("status") != "completed" or not isinstance(body.get("output"), list):
            raise RuntimeError(f"Incomplete model response: {body.get('status')}")
        return body["output"]


class DemoModel:
    """Deterministic call generator, NOT an LLM; supports offline integration tests."""

    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]:
        question = next(item["content"] for item in transcript if item.get("role") == "user")
        results = {item["call_id"]: json.loads(item["output"])
                   for item in transcript if item.get("type") == "function_call_output"}
        if "支付" not in question and "购买" not in question and "3.2.1" not in question:
            return [message("当前数据仅覆盖支付失败事故，无法凭现有证据判断这个问题。")]
        if "metrics-1" not in results:
            return [call("metrics-1", "payment_metrics", {"category": "支付失败"})]
        if "docs-1" not in results:
            return [call("docs-1", "search_incident_docs", {"query": question + " 支付 3.2.1 SDK 重试"})]
        metrics = results["metrics-1"].get("data", {})
        docs = results["docs-1"].get("data", {}).get("matches", [])
        if not metrics or not docs:
            return [message("证据不足，无法判断原因；请核查数据源和发布日志。")]
        return [message(
            f"支付失败投诉由 {metrics['previous_count']} 条增至 {metrics['current_count']} 条"
            f"（+{metrics['growth_rate_percent']}%）；其中 3.2.1 版本占"
            f" {next((v['count'] for v in metrics['versions'] if v['version']=='3.2.1'), 0)} 条。"
            f"文档 {docs[0]['source']} 提到相关 SDK / 重试配置变更。"
            "这些证据提示版本变更与投诉增加相关，尚不能证明因果；需进一步核对支付服务日志。"
        )]


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False)}


def message(content: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}
