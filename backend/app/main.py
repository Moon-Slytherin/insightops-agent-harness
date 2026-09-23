"""InsightOps FastAPI 应用入口。"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Literal

from backend.app.agent import investigate
from backend.app.analytics import dashboard_metrics
from backend.app.database import initialize_database
from backend.app.evaluator import run_evaluation
from backend.app.harness.runtime import Harness
from backend.app.harness.store import RunBusy, RunStore
from backend.app.harness.eval import evaluate
from backend.app.schemas import DashboardResponse, EvalReport, HealthResponse, InvestigationResponse, InvestigateRequest, ProjectResponse


ROOT = Path(__file__).resolve().parents[2]
initialize_database()

app = FastAPI(
    title="InsightOps Agent",
    description="从用户反馈和产品文档中调查产品事故并给出证据",
    version="1.0.0",
)

harness = Harness(RunStore())


class HarnessRequest(BaseModel):
    question: str = Field(min_length=4, max_length=300)
    mode: Literal["demo", "openai", "deepseek"] = "demo"


@app.post("/api/harness/runs")
async def create_harness_run(request: HarnessRequest) -> dict:
    try:
        return await harness.start(request.question, request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/harness/runs/{run_id}")
def get_harness_run(run_id: str) -> dict:
    run = harness.store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/api/harness/runs/{run_id}/events")
def get_harness_events(run_id: str) -> list[dict]:
    if harness.store.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return harness.store.events(run_id)


@app.post("/api/harness/runs/{run_id}/resume")
async def resume_harness_run(run_id: str) -> dict:
    try:
        return await harness.resume(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except RunBusy as exc:
        raise HTTPException(status_code=409, detail="run is already running") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/harness/evals")
async def get_harness_evals() -> dict:
    return await evaluate()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> str:
    return (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/project", response_model=ProjectResponse)
def get_project() -> ProjectResponse:
    return ProjectResponse(
        name="InsightOps Agent",
        tagline="会调查产品事故的 AI 侦探",
        stage="V1：证据优先的最小调查闭环",
        target_users=["产品经理", "用户运营", "质量团队"],
        demo_question="3.2.1 版本发布后，支付失败投诉为什么突然增加？",
    )


@app.get("/api/dashboard", response_model=DashboardResponse)
def get_dashboard() -> DashboardResponse:
    return DashboardResponse(**dashboard_metrics())


@app.post("/api/investigate", response_model=InvestigationResponse)
def run_investigation(request: InvestigateRequest) -> InvestigationResponse:
    return investigate(request.question, request.simulate_timeout)


@app.get("/api/evals", response_model=EvalReport)
def get_evaluation_report() -> EvalReport:
    return run_evaluation()
