"""The daily report: plain numbers and lists from the event log, no LLM (ADR-012).

`python -m axiom.report [PROJECT]`, or `make report`.
"""

import sys
from datetime import timedelta
from decimal import Decimal
from typing import Any

from axiom.config import get_settings
from axiom.metrics import Summary, TaskMetrics
from axiom.orchestrator import budgets, measures
from axiom.state.db import Conn, connect

NEEDS_YOU = {
    "AWAITING_MERGE": "merge it, comment `/axiom changes …`, or close the PR",
    "WAITING_FOR_HUMAN": "answer it, or cancel it",
    "FAILED": "raise its budget and retry, or cancel it",
}

TODAY_EVENTS = (
    "TASK_SUBMITTED",
    "REVIEW_SUBMITTED",
    "TASK_REOPENED",
    "TASK_DONE",
    "EXECUTION_FAILED",
    "EXECUTION_TIMED_OUT",
    "TASK_ESCALATED",
)
NONE = "-"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return NONE
    delta = timedelta(seconds=int(seconds))
    hours, rest = divmod(int(delta.total_seconds()), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def fmt_pct(value: float | None) -> str:
    return NONE if value is None else f"{value * 100:.0f}%"


def fmt_usd(value: Decimal | None) -> str:
    return NONE if value is None else f"${value:.2f}"


def render_report(
    tasks: list[TaskMetrics],
    summary: Summary,
    baselines: list[dict[str, Any]],
    *,
    today_spent: Decimal,
    daily_cap: Decimal,
    today_events: dict[str, int],
    title: str = "Axiom daily report",
) -> str:
    lines = [f"# {title}", ""]
    waiting = [t for t in tasks if t.state in NEEDS_YOU]
    lines += ["## Needs you", ""]
    if waiting:
        lines += [f"- **{t.key}** is {t.state}: {NEEDS_YOU[t.state]}" for t in waiting]
    else:
        lines.append("Nothing.")
    lines += ["", "## Today", "", f"- Spend: {fmt_usd(today_spent)} of {fmt_usd(daily_cap)}"]
    lines += [f"- {k}: {today_events[k]}" for k in TODAY_EVENTS if today_events.get(k)]
    rework = summary.rework_rounds_per_merged if summary.merged else NONE
    interventions = summary.human_interventions_per_merged if summary.merged else NONE
    lines += [
        "",
        "## Delivery so far",
        "",
        f"- Tasks: {summary.tasks}; merged: {summary.merged}",
        f"- Cost per merged task (all spend): {fmt_usd(summary.cost_per_merged_usd)}",
        f"- First-pass acceptance: {fmt_pct(summary.first_pass_rate)}",
        f"- Rework rounds per merged task: {rework}",
        f"- Median cycle time: {fmt_duration(summary.median_cycle_s)}",
        f"- Median waiting for a human: {fmt_duration(summary.median_waiting_for_human_s)} "
        "(elapsed time, an upper bound on attention)",
        f"- Human interventions per merged task: {interventions}",
        f"- Reviews' share of cost: {fmt_pct(summary.review_share_of_cost)}",
        f"- Review verdicts: {summary.verdicts or NONE} (a FAIL may be a false alarm)",
        f"- Defects found after merge: {summary.defects_after_merge}",
        f"- Merged before the gate: {summary.merged_before_gate}",
    ]
    lines += ["", "## Baselines (hand-driven, for comparison)", ""]
    if baselines:
        for b in baselines:
            p = b["payload"]
            rounds = p.get("review_rounds", 0)
            lines.append(f"- {p['title']}: {p['minutes']} min, ${p['cost_usd']}, {rounds} rounds")
    else:
        lines.append("None yet: without these, the main question can't be answered.")
    return "\n".join(lines) + "\n"


def build_report(conn: Conn, project_key: str | None = None) -> str:
    tasks, summary, baselines = measures.collect(conn, project_key=project_key)
    rows = conn.execute(
        """SELECT type, count(*) AS n FROM events
           WHERE occurred_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
           GROUP BY type"""
    ).fetchall()
    return render_report(
        tasks,
        summary,
        baselines,
        today_spent=budgets.daily_spent(conn),
        daily_cap=get_settings().daily_budget_usd,
        today_events={r["type"]: r["n"] for r in rows},
        title=f"Axiom daily report: {project_key}" if project_key else "Axiom daily report",
    )


def main() -> int:
    with connect(get_settings().database_url) as conn:
        print(build_report(conn, sys.argv[1] if len(sys.argv) > 1 else None), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
