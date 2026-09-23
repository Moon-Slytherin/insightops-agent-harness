"""Checkpointed function-calling loop with explicit budgets and recovery."""

import asyncio
import contextlib
import json
import time
from typing import Callable

import httpx

from backend.app.harness.model import DeepSeekResponses, DemoModel, Model, OpenAIResponses
from backend.app.harness.grounding import validate_answer
from backend.app.harness.store import LeaseLost, RunStore
from backend.app.harness.tools import TOOLS, execute


class Harness:
    def __init__(self, store: RunStore, model_factory: Callable[[str], Model] | None = None,
                 max_rounds: int = 6, max_tool_calls: int = 8, tool_timeout: float = 5.0,
                 lease_seconds: float = 30.0):
        self.store = store
        self.model_factory = model_factory or (lambda mode: {
            "demo": DemoModel, "openai": OpenAIResponses, "deepseek": DeepSeekResponses
        }[mode]())
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls
        self.tool_timeout = tool_timeout
        self.lease_seconds = lease_seconds

    async def start(self, question: str, mode: str = "demo") -> dict:
        if mode not in ("demo", "openai", "deepseek"):
            raise ValueError("mode must be demo, openai or deepseek")
        model = self.model_factory(mode)  # validate configuration before creating run
        run = self.store.create(question, mode)
        claimed, token = self.store.acquire(run["run_id"], self.lease_seconds)
        return await self._run_with_lease(claimed, model, token)

    async def resume(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["status"] in ("completed", "limit_reached"):
            return run
        model = self.model_factory(run["mode"])
        claimed, token = self.store.acquire(run_id, self.lease_seconds)
        return await self._run_with_lease(claimed, model, token)

    async def _run_with_lease(self, run: dict, model: Model, token: str) -> dict:
        if run["status"] in ("completed", "limit_reached"):
            self.store.release(run["run_id"], token)
            return run
        current = asyncio.current_task()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                try:
                    self.store.renew(run["run_id"], token, self.lease_seconds)
                except LeaseLost:
                    # The old worker must stop when another worker has taken over.
                    current.cancel()
                    return

        keepalive = asyncio.create_task(heartbeat())
        try:
            return await self._drive(run, model, token)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            self.store.release(run["run_id"], token)

    async def _drive(self, run: dict, model: Model, token: str) -> dict:
        while True:
            # A response is saved before any tool executes. A stopped process resumes here.
            pending = [item for item in run["transcript"] if item.get("type") == "function_call"
                       and not any(output.get("type") == "function_call_output"
                                   and output.get("call_id") == item.get("call_id")
                                   for output in run["transcript"])]
            if pending:
                for item in pending:
                    if run["tool_calls"] >= self.max_tool_calls:
                        return self._limit(run, "tool_call_budget", token)
                    for attempt in range(2):
                        try:
                            output = await asyncio.wait_for(
                                asyncio.to_thread(execute, item["name"], item["arguments"]),
                                timeout=self.tool_timeout)
                            break
                        except Exception as exc:
                            if attempt == 0:
                                self.store.save(run, "tool_retry", {"call_id": item["call_id"],
                                                "reason": type(exc).__name__}, token)
                            else:
                                output = json.dumps({"ok": False, "error": "tool_execution_failed",
                                                     "reason": type(exc).__name__})
                    run["transcript"].append({"type": "function_call_output",
                                               "call_id": item["call_id"], "output": output})
                    run["tool_calls"] += 1
                    self.store.save(run, "tool_result", {"call_id": item["call_id"],
                                 "name": item["name"], "result": json.loads(output)}, token)
                continue
            if run["rounds"] >= self.max_rounds:
                return self._limit(run, "model_round_budget", token)
            try:
                # One retry for transient model errors; checkpoint a failure for later resume.
                for attempt in range(2):
                    try:
                        model_started = time.perf_counter()
                        items = await model.respond(run["transcript"], [t.spec() for t in TOOLS.values()])
                        model_latency_ms = round((time.perf_counter() - model_started) * 1000, 2)
                        break
                    except Exception as exc:
                        # Configuration/billing failures cannot be fixed by an
                        # immediate retry. Preserve the run for a later resume.
                        if attempt or (isinstance(exc, httpx.HTTPStatusError)
                                       and exc.response.status_code in (400, 401, 402, 403)):
                            raise
                        self.store.save(run, "model_retry", {"attempt": 1}, token)
                        await asyncio.sleep(0.1)
                if not isinstance(items, list) or not items:
                    raise ValueError("empty model output")
                if len(json.dumps(items, ensure_ascii=False)) > 64000:
                    raise ValueError("model response too large")
                calls = [item for item in items if item.get("type") == "function_call"]
                if len(calls) != len({item.get("call_id") for item in calls}):
                    raise ValueError("duplicate call IDs in model output")
                existing_ids = {item["call_id"] for item in run["transcript"]
                                if item.get("type") == "function_call"}
                if any(item.get("call_id") in existing_ids for item in calls):
                    raise ValueError("reused call ID")
                if any(not isinstance(item.get("name"), str) or not isinstance(item.get("arguments"), str)
                       for item in calls):
                    raise ValueError("invalid function call")
                run["transcript"].extend(items)
                run["rounds"] += 1
                run["status"] = "running"
                run["error"] = None
                self.store.save(run, "model_response", {
                    "round": run["rounds"],
                    "call_ids": [item["call_id"] for item in calls],
                    "latency_ms": model_latency_ms,
                    "usage": getattr(model, "last_usage", {}) or {},
                }, token)
                if calls:
                    continue
                texts = [part.get("text", "") for item in items if item.get("type") == "message"
                         for part in item.get("content", []) if part.get("type") == "output_text"]
                if not any(texts):
                    raise ValueError("model produced neither a call nor a final answer")
                candidate = "\n".join(texts)
                issues = validate_answer(candidate)
                if issues:
                    feedback_count = sum(
                        item.get("role") == "user"
                        and str(item.get("content", "")).startswith("[EVIDENCE_VALIDATION]")
                        for item in run["transcript"]
                    )
                    self.store.save(run, "answer_rejected", {"issues": issues}, token)
                    if feedback_count >= 1:
                        run["status"] = "paused"
                        run["error"] = "answer_validation_failed: " + "; ".join(issues)
                        self.store.save(run, "paused", {"error": run["error"]}, token)
                        return run
                    run["transcript"].append({
                        "role": "user",
                        "content": (
                            "[EVIDENCE_VALIDATION] 最终答案未通过确定性校验："
                            + "；".join(issues)
                            + "。请仅依据已有工具结果重写答案，修正数字关系，不要调用新工具。"
                        ),
                    })
                    self.store.save(run, "answer_revision_requested", {"issues": issues}, token)
                    continue
                run["answer"] = candidate
                run["status"] = "completed"
                self.store.save(run, "completed", {"answer": run["answer"]}, token)
                return run
            except LeaseLost:
                raise
            except Exception as exc:
                run["status"] = "paused"
                run["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                self.store.save(run, "paused", {"error": run["error"]}, token)
                return run

    def _limit(self, run: dict, reason: str, token: str) -> dict:
        run["status"] = "limit_reached"
        run["error"] = reason
        self.store.save(run, "limit_reached", {"reason": reason}, token)
        return run
