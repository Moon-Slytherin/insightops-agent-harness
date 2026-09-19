"""业务指标查询工具。"""

from backend.app.database import query


CURRENT_START = "2026-08-03"
CURRENT_END = "2026-08-09"
PREVIOUS_START = "2026-07-27"
PREVIOUS_END = "2026-08-02"


def payment_incident_metrics() -> dict:
    period_rows = query(
        """
        SELECT
          SUM(CASE WHEN created_at BETWEEN ? AND ? THEN 1 ELSE 0 END) AS current_count,
          SUM(CASE WHEN created_at BETWEEN ? AND ? THEN 1 ELSE 0 END) AS previous_count
        FROM feedback
        WHERE category = '支付失败' AND is_duplicate = 0
        """,
        (CURRENT_START, CURRENT_END, PREVIOUS_START, PREVIOUS_END),
    )[0]
    current = int(period_rows["current_count"] or 0)
    previous = int(period_rows["previous_count"] or 0)
    growth = round((current - previous) / previous * 100, 1) if previous else 0.0

    versions = query(
        """
        SELECT version, COUNT(*) AS count
        FROM feedback
        WHERE category = '支付失败'
          AND created_at BETWEEN ? AND ?
          AND is_duplicate = 0
        GROUP BY version
        ORDER BY count DESC
        """,
        (CURRENT_START, CURRENT_END),
    )
    return {
        "current_count": current,
        "previous_count": previous,
        "growth_rate_percent": growth,
        "versions": versions,
    }


def dashboard_metrics() -> dict:
    totals = query(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN urgency = '紧急' THEN 1 ELSE 0 END) AS urgent,
               SUM(is_duplicate) AS duplicates
        FROM feedback
        """
    )[0]
    categories = query(
        """
        SELECT category, COUNT(*) AS count
        FROM feedback
        WHERE is_duplicate = 0
        GROUP BY category
        ORDER BY count DESC
        """
    )
    incident = payment_incident_metrics()
    return {
        "total_feedback": totals["total"],
        "urgent_feedback": totals["urgent"],
        "duplicate_feedback": totals["duplicates"],
        "current_period_payment_failures": incident["current_count"],
        "previous_period_payment_failures": incident["previous_count"],
        "growth_rate_percent": incident["growth_rate_percent"],
        "top_categories": categories,
        "payment_failures_by_version": incident["versions"],
    }
