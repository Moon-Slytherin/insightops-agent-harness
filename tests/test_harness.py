"""Integration tests for checkpoints, validation, budgets and API contracts."""

import asyncio
import json
import sqlite3

import pytest

from fastapi.testclient import TestClient
import httpx

from backend.app.harness.model import DeepSeekResponses, DemoModel, OpenAIResponses, call, message
from backend.app.harness.eval import CASES, check_case
from backend.app.harness.eval_deepseek import (CASES as DEEPSEEK_CASES, rescore_report,
                                               score_case, summarize)
from backend.app.harness.grounding import validate_answer
from backend.app.harness.runtime import Harness
from backend.app.harness.store import LeaseLost, RunBusy, RunStore
from backend.app.harness.tools import execute
from backend.app.main import app


QUESTION = "3.2.1 版本发布后支付失败投诉为什么增加？"
DEEPSEEK_TEST_KEY = "sk-test-deepseek-key-000000000000"


def test_multistep_loop_and_durable_trace(tmp_path):
    path = tmp_path / "runs.db"
    first = Harness(RunStore(path))
    run = asyncio.run(first.start(QUESTION))
    assert run["status"] == "completed"
    assert (run["rounds"], run["tool_calls"]) == (3, 2)
    assert "不能证明因果" in run["answer"]
    second = Harness(RunStore(path))
    assert asyncio.run(second.resume(run["run_id"])) == run
    assert [e["kind"] for e in second.store.events(run["run_id"])].count("tool_result") == 2


def test_eval_rejects_unbacked_number_even_if_keywords_match(tmp_path):
    case = json.loads(CASES.read_text(encoding="utf-8"))[0]
    run = asyncio.run(Harness(RunStore(tmp_path / "runs.db")).start(case["question"]))
    assert all(check_case(run, case).values())
    run["answer"] += "另外新增了 99 条投诉。"
    checks = check_case(run, case)
    assert checks["answer"] is True  # keyword-only evaluation would miss this
    assert checks["evidence_match"] is False


def test_eval_rejects_missing_tool_result_even_if_answer_looks_correct(tmp_path):
    case = json.loads(CASES.read_text(encoding="utf-8"))[0]
    run = asyncio.run(Harness(RunStore(tmp_path / "runs.db")).start(case["question"]))
    run["transcript"] = [item for item in run["transcript"]
                         if not (item.get("type") == "function_call_output"
                                 and item.get("call_id") == "docs-1")]
    checks = check_case(run, case)
    assert checks["answer"] is True
    assert checks["tool_results"] is False
    assert checks["evidence_match"] is False


def test_pending_function_call_survives_process_restart(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    run = store.create(QUESTION, "demo")
    run["transcript"].append(call("metrics-1", "payment_metrics", {"category": "支付失败"}))
    run["rounds"] = 1
    store.save(run, "model_response", {"round": 1})
    resumed = asyncio.run(Harness(RunStore(path)).resume(run["run_id"]))
    assert resumed["status"] == "completed"
    assert resumed["tool_calls"] == 2
    assert [i["call_id"] for i in resumed["transcript"] if i.get("type") == "function_call_output"] == ["metrics-1", "docs-1"]


def test_model_error_pauses_and_later_resumes(tmp_path):
    class BrokenOnce:
        def __init__(self):
            self.attempts = 0

        async def respond(self, transcript, tools):
            self.attempts += 1
            if self.attempts <= 2:
                raise TimeoutError("temporarily unavailable")
            return await DemoModel().respond(transcript, tools)

    store = RunStore(tmp_path / "runs.db")
    model = BrokenOnce()
    harness = Harness(store, lambda mode: model)
    run = asyncio.run(harness.start(QUESTION))
    assert run["status"] == "paused"
    assert run["rounds"] == 0
    assert asyncio.run(harness.resume(run["run_id"]))["status"] == "completed"
    assert any(e["kind"] == "model_retry" for e in store.events(run["run_id"]))


def test_tool_failure_retries_with_event(tmp_path, monkeypatch):
    from backend.app.harness import runtime

    real_execute = runtime.execute
    attempts = 0

    def flaky(name, arguments):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("test timeout")
        return real_execute(name, arguments)

    monkeypatch.setattr(runtime, "execute", flaky)
    store = RunStore(tmp_path / "runs.db")
    run = asyncio.run(Harness(store).start(QUESTION))
    assert run["status"] == "completed"
    assert attempts == 3
    assert any(event["kind"] == "tool_retry" for event in store.events(run["run_id"]))


def test_schema_and_tool_allowlist():
    assert json.loads(execute("payment_metrics", '{"category":"支付失败","sql":"DELETE FROM feedback"}'))["ok"] is False
    assert json.loads(execute("shell", '{"command":"rm -rf /"}'))["error"] == "unknown_tool"
    assert json.loads(execute("payment_metrics", "not json"))["ok"] is False


def test_loop_budget_stops_unbounded_calls(tmp_path):
    harness = Harness(RunStore(tmp_path / "runs.db"), max_rounds=1)
    run = asyncio.run(harness.start(QUESTION))
    assert run["status"] == "limit_reached"
    assert run["tool_calls"] == 1
    assert run["error"] == "model_round_budget"


def test_api_trace_eval_and_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = TestClient(app)
    response = client.post("/api/harness/runs", json={"question": QUESTION})
    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "completed"
    assert client.get(f'/api/harness/runs/{run["run_id"]}').json()["answer"] == run["answer"]
    assert any(item["kind"] == "tool_result" for item in client.get(f'/api/harness/runs/{run["run_id"]}/events').json())
    report = client.get("/api/harness/evals").json()
    assert report["total"] == report["passed"] == 4
    assert client.post("/api/harness/runs", json={"question": QUESTION, "mode": "openai"}).status_code == 400
    assert client.post("/api/harness/runs", json={"question": QUESTION, "mode": "deepseek"}).status_code == 400


def test_two_workers_cannot_resume_the_same_run_at_once(tmp_path):
    async def scenario():
        started = asyncio.Event()
        proceed = asyncio.Event()
        calls = 0

        class SlowModel:
            async def respond(self, transcript, tools):
                nonlocal calls
                calls += 1
                started.set()
                await proceed.wait()
                return await DemoModel().respond(transcript, tools)

        path = tmp_path / "runs.db"
        first = Harness(RunStore(path), lambda mode: SlowModel())
        task = asyncio.create_task(first.start(QUESTION))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            with sqlite3.connect(path) as db:
                run_id = db.execute("SELECT id FROM runs").fetchone()[0]
            with pytest.raises(RunBusy):
                await Harness(RunStore(path)).resume(run_id)
        finally:
            proceed.set()
        result = await task
        assert result["status"] == "completed"
        assert calls == 3
        assert len([e for e in RunStore(path).events(run_id) if e["kind"] == "tool_result"]) == 2

    asyncio.run(scenario())


def test_expired_lease_can_be_recovered_but_old_owner_cannot_write(tmp_path):
    path = tmp_path / "runs.db"
    store = RunStore(path)
    run = store.create(QUESTION, "demo")
    _, old_token = store.acquire(run["run_id"])
    # Simulate a crashed worker's expired lease without waiting for real time.
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (run["run_id"],))
    _, new_token = RunStore(path).acquire(run["run_id"])
    with pytest.raises(LeaseLost):
        store.save(run, "obsolete_result", {}, old_token)
    store.release(run["run_id"], old_token)
    with pytest.raises(RunBusy):
        store.acquire(run["run_id"])
    assert "obsolete_result" not in [e["kind"] for e in store.events(run["run_id"])]
    store.release(run["run_id"], new_token)
    result = asyncio.run(Harness(RunStore(path)).resume(run["run_id"]))
    assert result["status"] == "completed"


def test_store_upgrades_existing_checkpoint_table(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
    store = RunStore(path)
    run = store.create(QUESTION, "demo")
    assert asyncio.run(Harness(store).resume(run["run_id"]))["status"] == "completed"


def test_store_connection_closes_before_temp_database_cleanup(tmp_path):
    store = RunStore(tmp_path / "temporary.db")
    with store.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    # SQLite's `with connection` commits but does not close it. On Windows,
    # leaving it open locks the file and breaks TemporaryDirectory cleanup.
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    (tmp_path / "temporary.db").unlink()


def test_busy_resume_returns_http_409(tmp_path, monkeypatch):
    from backend.app import main

    store = RunStore(tmp_path / "runs.db")
    run = store.create(QUESTION, "demo")
    _, token = store.acquire(run["run_id"])
    monkeypatch.setattr(main, "harness", Harness(store))
    try:
        response = TestClient(app).post(f'/api/harness/runs/{run["run_id"]}/resume')
        assert response.status_code == 409
        assert response.json()["detail"] == "run is already running"
    finally:
        store.release(run["run_id"], token)


def test_slow_model_keeps_lease_alive(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        class SlowModel:
            async def respond(self, transcript, tools):
                entered.set()
                await asyncio.sleep(0.3)
                return await DemoModel().respond(transcript, tools)

        path = tmp_path / "runs.db"
        store = RunStore(path)
        worker = Harness(store, lambda mode: SlowModel(), lease_seconds=0.15)
        task = asyncio.create_task(worker.start(QUESTION))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            with store.connect() as db:
                run_id = db.execute("SELECT id FROM runs").fetchone()[0]
            await asyncio.sleep(0.22)  # longer than original lease lifetime
            with pytest.raises(RunBusy):
                await Harness(RunStore(path)).resume(run_id)
        finally:
            await task
        assert store.get(run_id)["status"] == "completed"

    asyncio.run(scenario())


def test_responses_adapter_contract_without_network(monkeypatch):
    from backend.app.harness import model as model_module
    from backend.app.harness.tools import TOOLS

    captured = []
    reasoning = {"type": "reasoning", "id": "rs_123", "summary": []}
    function_call = call("call_123", "payment_metrics", {"category": "支付失败"})
    original_client = httpx.AsyncClient

    def mock_handler(request):
        assert request.url.path == "/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        captured.append(body)
        assert body["store"] is False
        assert all(tool["strict"] is True for tool in body["tools"])
        if len(captured) == 1:
            return httpx.Response(200, json={"status": "completed", "output": [reasoning, function_call]})
        assert body["input"] == [{"role": "user", "content": QUESTION}, reasoning,
                                  function_call, {"type": "function_call_output",
                                                  "call_id": "call_123", "output": '{"ok":true}'}]
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": []}]})

    monkeypatch.setattr(model_module.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(mock_handler), **kwargs))
    adapter = OpenAIResponses(api_key="test-key")
    tools = [tool.spec() for tool in TOOLS.values()]

    async def scenario():
        items = await adapter.respond([{"role": "user", "content": QUESTION}], tools)
        transcript = [{"role": "user", "content": QUESTION}, *items,
                      {"type": "function_call_output", "call_id": "call_123", "output": '{"ok":true}'}]
        await adapter.respond(transcript, tools)

    asyncio.run(scenario())
    assert len(captured) == 2


def test_deepseek_chat_tool_round_trip_without_network(tmp_path, monkeypatch):
    from backend.app.harness import model as model_module

    captured = []
    original_client = httpx.AsyncClient

    def mock_handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {DEEPSEEK_TEST_KEY}"
        body = json.loads(request.content)
        captured.append(body)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["reasoning_effort"] == "none"
        assert body["max_tokens"] == 1024
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1] == {"role": "user", "content": QUESTION}
        assert all(set(tool) == {"type", "function"} for tool in body["tools"])
        if len(captured) == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "ds-1", "type": "function", "function": {
                        "name": "payment_metrics",
                        "arguments": '{"category":"支付失败"}'}}]}}]})
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["tool_call_id"] == "ds-1"
        assert json.loads(body["messages"][-1]["content"])["ok"] is True
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "模拟回答，仅用于验证调用链。"}}]})

    monkeypatch.setattr(model_module.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(mock_handler), **kwargs))
    harness = Harness(RunStore(tmp_path / "runs.db"),
                      lambda mode: DeepSeekResponses(api_key=DEEPSEEK_TEST_KEY))
    result = asyncio.run(harness.start(QUESTION, mode="deepseek"))
    assert result["status"] == "completed"
    assert result["tool_calls"] == 1
    assert result["mode"] == "deepseek"
    assert len(captured) == 2


def test_deepseek_billing_error_pauses_and_can_resume_without_network(tmp_path, monkeypatch):
    from backend.app.harness import model as model_module

    attempts = 0
    original_client = httpx.AsyncClient

    def mock_handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(402, json={"error": "Insufficient Balance"})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "恢复后的模拟回答。"}}]})

    monkeypatch.setattr(model_module.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(mock_handler), **kwargs))
    store = RunStore(tmp_path / "runs.db")
    harness = Harness(store, lambda mode: DeepSeekResponses(api_key=DEEPSEEK_TEST_KEY))
    paused = asyncio.run(harness.start(QUESTION, mode="deepseek"))
    assert paused["status"] == "paused"
    assert paused["rounds"] == 0
    assert attempts == 1
    assert DEEPSEEK_TEST_KEY not in paused["error"]
    resumed = asyncio.run(harness.resume(paused["run_id"]))
    assert resumed["status"] == "completed"
    assert resumed["mode"] == "deepseek"
    assert attempts == 2


def test_deepseek_rejects_truncated_key_before_network_call():
    with pytest.raises(ValueError, match="format looks invalid"):
        DeepSeekResponses(api_key="x")


def test_numeric_validator_catches_subset_larger_than_total():
    answer = "本期新增的 13 条投诉里，16 条（约 89%）来自 3.2.1。"
    assert validate_answer(answer) == ["数量关系矛盾：子集 16 条不能大于所述总数 13 条"]
    assert validate_answer("本期 18 条投诉里，16 条来自 3.2.1。") == []


def test_invalid_answer_is_rejected_and_revised(tmp_path):
    class RevisingModel:
        async def respond(self, transcript, tools):
            results = [item for item in transcript
                       if item.get("type") == "function_call_output"]
            if len(results) < 2:
                return await DemoModel().respond(transcript, tools)
            feedback = any(str(item.get("content", "")).startswith("[EVIDENCE_VALIDATION]")
                           for item in transcript if item.get("role") == "user")
            if not feedback:
                return [message("本期新增的 13 条投诉里，16 条来自 3.2.1。")]
            return [message("本期共 18 条投诉，其中 16 条来自 3.2.1；仍不能证明因果。")]

    store = RunStore(tmp_path / "runs.db")
    run = asyncio.run(Harness(store, lambda mode: RevisingModel()).start(QUESTION))
    assert run["status"] == "completed"
    assert "共 18 条" in run["answer"]
    kinds = [event["kind"] for event in store.events(run["run_id"])]
    assert "answer_rejected" in kinds
    assert "answer_revision_requested" in kinds


def test_repeated_invalid_answer_pauses_instead_of_publishing(tmp_path):
    class AlwaysInvalid:
        async def respond(self, transcript, tools):
            results = [item for item in transcript
                       if item.get("type") == "function_call_output"]
            if len(results) < 2:
                return await DemoModel().respond(transcript, tools)
            return [message("本期新增的 13 条投诉里，16 条来自 3.2.1。")]

    store = RunStore(tmp_path / "runs.db")
    run = asyncio.run(Harness(store, lambda mode: AlwaysInvalid()).start(QUESTION))
    assert run["status"] == "paused"
    assert run["answer"] is None
    assert run["error"].startswith("answer_validation_failed")


def _scored_run(answer, tools=(), status="completed"):
    transcript = [{"role": "user", "content": "test"}]
    for index, name in enumerate(tools):
        call_id = f"call-{index}"
        transcript.extend([
            call(call_id, name, {"category": "支付失败"} if name == "payment_metrics"
                 else {"query": "3.2.1 支付"}),
            {"type": "function_call_output", "call_id": call_id,
             "output": json.dumps({"ok": True, "data": {}}, ensure_ascii=False)},
        ])
    return {"status": status, "answer": answer, "transcript": transcript,
            "rounds": 2, "tool_calls": len(tools), "error": None}


def test_live_eval_scores_observable_tools_facts_and_policy():
    case = {
        "id": "sample", "question": "sample",
        "expected_tools": ["payment_metrics", "search_incident_docs"],
        "tool_match": "exact",
        "required_facts": ["current_count", "previous_count", "version_3_2_1", "sdk_or_retry"],
        "policy": "causal_caution",
    }
    answer = ("本期共 18 条，上期为 5 条；3.2.1 版本有 16 条。"
              "发布说明提到 SDK 变更，但现有证据不足以证明因果。")
    result = score_case(_scored_run(answer, ("payment_metrics", "search_incident_docs")), case)
    assert result["passed"] is True
    assert all(result["checks"].values())


def test_live_eval_exposes_failure_dimensions_instead_of_one_score():
    case = {
        "id": "sample", "question": "sample", "expected_tools": ["payment_metrics"],
        "tool_match": "exact", "required_facts": ["current_count"],
        "forbidden_phrases": ["500 条支付失败投诉"], "policy": "correct_false_premise",
    }
    result = score_case(_scored_run("本期有 500 条支付失败投诉。", ()), case)
    assert result["passed"] is False
    assert result["checks"]["completed"] is True
    assert result["checks"]["tool_selection"] is False
    assert result["checks"]["required_facts"] is False
    assert result["checks"]["policy"] is False
    assert result["checks"]["forbidden_phrases"] is False


def test_live_eval_dataset_and_summary_are_stable():
    cases = json.loads(DEEPSEEK_CASES.read_text(encoding="utf-8"))
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == 20
    assert all(set(case["expected_tools"]) <= {"payment_metrics", "search_incident_docs"}
               for case in cases)
    rows = [
        {"id": "pass", "passed": True, "checks": {"completed": True, "policy": True}},
        {"id": "fail", "passed": False, "checks": {"completed": True, "policy": False}},
    ]
    report = summarize(rows)
    assert report["passed"] == 1
    assert report["case_pass_rate"] == 0.5
    assert report["metrics"]["completed"]["rate"] == 1.0
    assert report["performance"]["total_tokens"] == 0
    assert report["failed_case_ids"] == ["fail"]


@pytest.mark.parametrize("answer", [
    "支付失败投诉当期 18 起，上一周期 5 起；3.2.1 = 16 起，3.2.0 = 2 起。",
    "| 期间 | 投诉条数 |\n|---|---|\n| 本期 | 18 |\n| 上期 | 5 |\n3.2.1：16 条，3.2.0：2 条。",
    "| 指标 | 数值 |\n|---|---|\n| 本期支付失败投诉 | **18** |\n| 上期投诉 | **5** |\n3.2.1：16 条，3.2.0：2 条。",
    "本期 **18**，上期 **5**；版本分布 | 3.2.1：**16**；3.2.0：**2** |",
])
def test_live_eval_accepts_equivalent_count_wording(answer):
    case = {
        "id": "wording", "question": "wording", "expected_tools": [],
        "tool_match": "exact",
        "required_facts": ["current_count", "previous_count", "version_3_2_1", "version_3_2_0"],
        "policy": "factual",
    }
    result = score_case(_scored_run(answer), case)
    assert result["checks"]["required_facts"] is True


def test_live_eval_collects_latency_and_provider_token_usage():
    case = {"id": "perf", "question": "perf", "expected_tools": [],
            "tool_match": "exact", "required_facts": [], "policy": "factual"}
    events = [
        {"kind": "model_response", "detail": {"latency_ms": 120.5,
         "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}},
        {"kind": "model_response", "detail": {"latency_ms": 80,
         "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26}}},
    ]
    row = score_case(_scored_run("回答"), case, events)
    assert row["performance"] == {"model_latency_ms": 200.5, "prompt_tokens": 30,
                                  "completion_tokens": 10, "total_tokens": 40}


def test_live_eval_accepts_out_of_scope_refusal_wording():
    case = {"id": "refusal", "question": "refusal", "expected_tools": [],
            "tool_match": "exact", "required_facts": [], "policy": "unsupported"}
    row = score_case(_scored_run("考勤异常不在我可用的工具范围内。"), case)
    assert row["checks"]["policy"] is True


def test_live_eval_accepts_not_causal_wording():
    case = {"id": "causal", "question": "causal", "expected_tools": [],
            "tool_match": "exact", "required_facts": [], "policy": "causal_caution"}
    row = score_case(_scored_run("这些证据只说明相关性，目前不构成因果证明。"), case)
    assert row["checks"]["policy"] is True
    row = score_case(_scored_run("因果关系尚不能确认。"), case)
    assert row["checks"]["policy"] is True


def test_live_eval_can_allow_multiple_valid_tool_paths():
    case = {"id": "paths", "question": "paths", "expected_tools": [],
            "allowed_tools": ["search_incident_docs"], "tool_match": "allowed",
            "required_facts": [], "policy": "insufficient_evidence"}
    assert score_case(_scored_run("缺少平台数据，无法确认。"), case)["passed"] is True
    assert score_case(_scored_run("缺少平台数据，无法确认。",
                                  ("search_incident_docs",)), case)["passed"] is True
    assert score_case(_scored_run("缺少平台数据，无法确认。",
                                  ("payment_metrics",)), case)["checks"]["tool_selection"] is False


def test_saved_report_can_be_rescored_without_model_or_raw_tool_outputs():
    case = {"id": "saved", "question": "saved", "expected_tools": [],
            "tool_match": "exact", "required_facts": [], "policy": "causal_caution"}
    saved = {"generated_at": "2026-09-23T00:00:00Z", "cases": [{
        "id": "saved", "status": "completed", "tools": [],
        "answer": "现有证据不构成因果证明。",
        "checks": {"tool_execution": True},
        "performance": {"model_latency_ms": 10, "total_tokens": 20},
    }]}
    report = rescore_report(saved, [case])
    assert report["schema_version"] == 2
    assert report["summary"]["passed"] == 1
    assert report["summary"]["performance"]["total_tokens"] == 20
