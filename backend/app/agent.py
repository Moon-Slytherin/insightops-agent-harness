"""证据优先的最小 Agent 工作流。"""

import time
import uuid

from backend.app.analytics import payment_incident_metrics
from backend.app.retriever import search_knowledge
from backend.app.schemas import Evidence, InvestigationResponse, ToolStep


SUPPORTED_TERMS = ("支付", "3.2.1", "版本")


def investigate(question: str, simulate_timeout: bool = False) -> InvestigationResponse:
    run_id = uuid.uuid4().hex[:10]
    trace = [ToolStep(step=1, tool="intent_router", status="success", summary="识别为产品事故归因问题")]

    if not any(term in question.lower() for term in SUPPORTED_TERMS):
        trace.append(ToolStep(step=2, tool="tool_selector", status="skipped", summary="当前 V1 不支持该问题"))
        return InvestigationResponse(
            run_id=run_id,
            question=question,
            answer="当前 V1 只支持支付失败事故调查。为避免无证据猜测，本次不生成原因结论。",
            confidence="low",
            evidence=[],
            trace=trace,
            limitations=["V1 仅覆盖固定演示事故", "未找到可核验证据时拒绝猜测"],
        )

    if simulate_timeout:
        trace.append(ToolStep(step=2, tool="sql_analytics", status="retry", summary="首次调用模拟超时，已自动重试 1 次"))
        time.sleep(0.01)

    metrics = payment_incident_metrics()
    trace.append(ToolStep(step=3 if simulate_timeout else 2, tool="sql_analytics", status="success", summary="完成前后两周趋势和版本分布查询"))
    documents = search_knowledge(question)
    trace.append(ToolStep(step=4 if simulate_timeout else 3, tool="knowledge_search", status="success", summary=f"命中 {len(documents)} 份产品文档"))

    top_version = metrics["versions"][0] if metrics["versions"] else {"version": "未知", "count": 0}
    current = metrics["current_count"]
    previous = metrics["previous_count"]
    growth = metrics["growth_rate_percent"]
    evidence = [
        Evidence(
            source_type="sql",
            title="支付失败投诉趋势",
            detail=f"8月3日至9日共 {current} 条，前一周 {previous} 条，增长 {growth}%（已排除重复反馈）。",
            source="SQLite / feedback 表，只读聚合查询",
        ),
        Evidence(
            source_type="sql",
            title="版本分布",
            detail=f"当前周期中 {top_version['version']} 版本有 {top_version['count']} 条支付失败投诉，数量最高。",
            source="SQLite / feedback 表，按 version 分组",
        ),
    ]
    evidence.extend(
        Evidence(source_type="knowledge", title=doc["title"], detail=doc["snippet"], source=doc["source"])
        for doc in documents
    )
    answer = (
        f"支付失败投诉从前一周的 {previous} 条增加到 {current} 条，增幅 {growth}%；"
        f"新增问题主要集中在 {top_version['version']} 版本（{top_version['count']} 条）。"
        "版本说明显示该版本在同一天调整了 Android 支付 SDK 的失败重试配置，"
        "因此该变更是当前最值得优先排查的原因。该结论是相关性判断，仍需结合支付服务端日志确认因果。"
    )
    trace.append(ToolStep(step=5 if simulate_timeout else 4, tool="evidence_synthesizer", status="success", summary="生成带来源和不确定性声明的结论"))
    return InvestigationResponse(
        run_id=run_id,
        question=question,
        answer=answer,
        confidence="medium",
        evidence=evidence,
        trace=trace,
        limitations=["数据为求职演示用模拟数据", "文档变更与投诉增长仅构成相关性证据", "需用服务端错误日志进一步验证因果"],
    )
