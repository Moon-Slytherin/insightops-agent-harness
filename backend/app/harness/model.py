"""Provider adapters and an explicit offline scripted model for repeatable demos."""

import json
import os
from typing import Protocol

import httpx


SYSTEM = (
    "你是 InsightOps 事故调查助手。请先按任务类型选择最小必要工具并直接执行，不要反问用户是否要查询："
    "询问投诉数量、增长率、版本分布或要求核实用户给出的投诉数字，只调用 payment_metrics；"
    "询问发布变更或事故手册，只调用 search_incident_docs；"
    "询问事故原因、证据链、SDK/重试配置的因果、下一步缺什么证据时，调用两个工具综合判断；"
    "询问退款到账、会员开通、具体代码、错误码、安装量、真实失败率、单独的平台数量或无关业务时，"
    "工具说明已表明无法回答，应直接说明缺少数据且不调用工具。"
    "如果问题是判断已知发布变更能否支持某个平台结论，只检索发布文档，不调用缺少平台维度的统计工具。"
    "不要为了补充背景而顺带调用无关工具。"
    "回答必须基于工具实际返回的数字和文档，区分相关性与因果性，不接受用户要求你捏造结论。"
    "你不能运行 Shell、修改文件或访问未提供的数据，只可使用本地给定的只读工具。"
)


class Model(Protocol):
    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]: ...


class OpenAIResponses:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.url = "https://api.openai.com/v1/responses"
        self.last_usage: dict = {}
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for openai mode")

    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]:
        self.last_usage = {}
        payload = {"model": self.model, "instructions": SYSTEM, "input": transcript,
                   "tools": self.provider_tools(tools), "tool_choice": "auto",
                   "parallel_tool_calls": False, "store": False}
        payload.update(self.extra_parameters())
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if response.is_error:
                # httpx's default error omits the provider response body, which
                # usually contains the actionable invalid-field explanation.
                detail = response.text[:1000]
                raise httpx.HTTPStatusError(
                    f"Provider HTTP {response.status_code}: {detail}",
                    request=response.request,
                    response=response,
                )
            body = response.json()
        self.last_usage = body.get("usage") or {}
        if body.get("status") != "completed" or not isinstance(body.get("output"), list):
            raise RuntimeError(f"Incomplete model response: {body.get('status')}")
        return body["output"]

    def provider_tools(self, tools: list[dict]) -> list[dict]:
        return tools

    def extra_parameters(self) -> dict:
        return {}


class DeepSeekResponses:
    """DeepSeek Chat Completions adapter translated to the harness item format."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.url = "https://api.deepseek.com/chat/completions"
        self.last_usage: dict = {}
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for deepseek mode")
        if len(self.api_key) < 20 or not self.api_key.startswith("sk-"):
            raise ValueError("DEEPSEEK_API_KEY format looks invalid; expected a full sk-... key")

    async def respond(self, transcript: list[dict], tools: list[dict]) -> list[dict]:
        self.last_usage = {}
        payload = {
            "model": self.model,
            "messages": self._messages(transcript),
            "tools": [self._tool(tool) for tool in tools],
            "tool_choice": "auto",
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
            "max_tokens": 1024,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if response.is_error:
                raise httpx.HTTPStatusError(
                    f"Provider HTTP {response.status_code}: {response.text[:1000]}",
                    request=response.request,
                    response=response,
                )
            body = response.json()
        self.last_usage = body.get("usage") or {}
        try:
            reply = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Invalid DeepSeek response") from exc
        calls = []
        for item in reply.get("tool_calls") or []:
            function = item.get("function") or {}
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append({"type": "function_call", "call_id": item.get("id"),
                          "name": function.get("name"), "arguments": arguments})
        if calls:
            return calls
        content = reply.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("DeepSeek produced neither tool calls nor text")
        return [message(content)]

    @staticmethod
    def _tool(tool: dict) -> dict:
        return {"type": "function", "function": {
            "name": tool["name"], "description": tool["description"],
            "parameters": tool["parameters"],
        }}

    @staticmethod
    def _messages(transcript: list[dict]) -> list[dict]:
        messages = [{"role": "system", "content": SYSTEM}]
        index = 0
        while index < len(transcript):
            item = transcript[index]
            if item.get("role") == "user":
                messages.append({"role": "user", "content": item["content"]})
            elif item.get("type") == "function_call":
                tool_calls = []
                while index < len(transcript) and transcript[index].get("type") == "function_call":
                    current = transcript[index]
                    tool_calls.append({"id": current["call_id"], "type": "function",
                                       "function": {"name": current["name"],
                                                    "arguments": current["arguments"]}})
                    index += 1
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": tool_calls})
                continue
            elif item.get("type") == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"],
                                 "content": item["output"]})
            elif item.get("type") == "message":
                text = "\n".join(part.get("text", "") for part in item.get("content", [])
                                 if part.get("type") == "output_text")
                if text:
                    messages.append({"role": "assistant", "content": text})
            index += 1
        return messages


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
