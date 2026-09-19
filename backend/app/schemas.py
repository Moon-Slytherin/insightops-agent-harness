"""InsightOps API 的数据模型。"""

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ProjectResponse(BaseModel):
    name: str
    tagline: str
    stage: str
    target_users: list[str]
    demo_question: str


class CategoryMetric(BaseModel):
    category: str
    count: int


class VersionMetric(BaseModel):
    version: str
    count: int


class DashboardResponse(BaseModel):
    total_feedback: int
    urgent_feedback: int
    duplicate_feedback: int
    current_period_payment_failures: int
    previous_period_payment_failures: int
    growth_rate_percent: float
    top_categories: list[CategoryMetric]
    payment_failures_by_version: list[VersionMetric]


class InvestigateRequest(BaseModel):
    question: str = Field(min_length=4, max_length=300)
    simulate_timeout: bool = False


class Evidence(BaseModel):
    source_type: Literal["sql", "knowledge"]
    title: str
    detail: str
    source: str


class ToolStep(BaseModel):
    step: int
    tool: str
    status: Literal["success", "retry", "skipped"]
    summary: str


class InvestigationResponse(BaseModel):
    run_id: str
    question: str
    answer: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence]
    trace: list[ToolStep]
    limitations: list[str]


class EvalCaseResult(BaseModel):
    id: str
    question: str
    passed: bool
    checks: dict[str, bool]


class EvalReport(BaseModel):
    total: int
    passed: int
    pass_rate_percent: float
    cases: list[EvalCaseResult]
