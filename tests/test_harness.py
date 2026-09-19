"""Integration tests for checkpoints, validation, budgets and API contracts."""

import asyncio
import json
import sqlite3

import pytest

from fastapi.testclient import TestClient
import httpx

from backend.app.harness.model import DemoModel, OpenAIResponses, call
from backend.app.harness.eval import CASES, check_case
from backend.app.harness.runtime import Harness
from backend.app.harness.store import LeaseLost, RunBusy, RunStore
from backend.app.harness.tools import execute
from backend.app.main import app


QUESTION = "3.2.1 版本发布后支付失败投诉为什么增加？"


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
