"""Opt-in, real API smoke check. Run only with an OPENAI_API_KEY set locally."""

import asyncio
import os
import tempfile
from pathlib import Path

from backend.app.harness.runtime import Harness
from backend.app.harness.store import RunStore


QUESTION = "3.2.1 版本发布后支付失败投诉为什么增加？请先查询统计和产品文档，说明证据与不确定性。"


async def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        print("请先在本机设置 OPENAI_API_KEY；不要把密钥写入仓库。")
        return 2
    with tempfile.TemporaryDirectory() as temp:
        store = RunStore(Path(temp) / "real_api_smoke.db")
        run = await Harness(store).start(QUESTION, mode="openai")
        names = [item["name"] for item in run["transcript"]
                 if item.get("type") == "function_call"]
        print(f"status={run['status']} rounds={run['rounds']} tool_calls={run['tool_calls']}")
        print(f"tools={names}")
        if run["status"] != "completed":
            print(f"error={run['error']}")
            return 1
        print(f"answer={run['answer']}")
        expected = {"payment_metrics", "search_incident_docs"}
        if not expected.issubset(names):
            print("未调用完整的演示工具链，需检查模型轨迹及提示词。")
            return 1
        print("真实 API 工具调用链完成；答案事实与引用仍需人工检查。")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
