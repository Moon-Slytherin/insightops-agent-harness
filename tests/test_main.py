"""核心接口和可靠性行为测试。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app.database import connect, query
from backend.app.main import app


client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_is_visible() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "InsightOps Agent" in response.text


def test_project_story_is_visible() -> None:
    body = client.get("/api/project").json()
    assert body["name"] == "InsightOps Agent"
    assert "支付失败" in body["demo_question"]
    assert "V1" in body["stage"]


def test_dashboard_shows_incident_growth() -> None:
    body = client.get("/api/dashboard").json()
    assert body["total_feedback"] == 45
    assert body["previous_period_payment_failures"] == 5
    assert body["current_period_payment_failures"] == 18
    assert body["growth_rate_percent"] == 260.0


def test_investigation_returns_evidence_and_trace() -> None:
    response = client.post("/api/investigate", json={"question": "3.2.1版本发布后支付失败投诉为什么增加？"})
    body = response.json()
    assert response.status_code == 200
    assert "260.0%" in body["answer"]
    assert len(body["evidence"]) >= 3
    assert {step["tool"] for step in body["trace"]} >= {"sql_analytics", "knowledge_search"}


def test_unsupported_question_refuses_to_guess() -> None:
    body = client.post("/api/investigate", json={"question": "退款为什么没有到账？"}).json()
    assert body["confidence"] == "low"
    assert body["evidence"] == []
    assert "不生成原因结论" in body["answer"]


def test_timeout_simulation_records_retry() -> None:
    body = client.post("/api/investigate", json={"question": "支付失败为什么增加？", "simulate_timeout": True}).json()
    assert any(step["status"] == "retry" for step in body["trace"])
    assert body["evidence"]


def test_database_rejects_write_queries() -> None:
    try:
        query("DELETE FROM feedback")
    except ValueError as error:
        assert "只允许 SELECT" in str(error)
    else:
        raise AssertionError("写查询必须被拒绝")


def test_feedback_connection_is_closed_after_use() -> None:
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_eval_report_exposes_known_failure() -> None:
    body = client.get("/api/evals").json()
    assert body["total"] == 12
    assert body["passed"] == 11
    assert body["pass_rate_percent"] == 91.7
