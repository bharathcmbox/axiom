"""Metrics, computed from what the system already records (ADR-012). Pure functions, no I/O.

Durations come from the task's transition events: the time between entering a state and leaving it.
Caveat shown wherever it is displayed: `waiting_for_human_s` is elapsed time, an upper bound on
attention, not a measure of attention.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any

AGENT_STATES = frozenset({"ASSIGNED", "IN_PROGRESS"})
REVIEW_STATES = frozenset({"IN_REVIEW"})
QUEUE_STATES = frozenset({"PENDING", "READY"})
HUMAN_STATES = frozenset({"AWAITING_MERGE", "WAITING_FOR_HUMAN", "FAILED"})
# Human events that are the normal flow, not an intervention.
ROUTINE_HUMAN_EVENTS = frozenset({"TASK_CREATED", "TASK_DONE", "DEFECT_RECORDED", "TASK_TAGGED"})
FAILURE_EVENTS = frozenset({"EXECUTION_FAILED", "EXECUTION_TIMED_OUT"})


@dataclass(frozen=True)
class TaskMetrics:
    key: str
    state: str
    merged: bool
    tags: tuple[str, ...]
    cycle_s: float | None  # first READY -> DONE
    agent_s: float
    review_s: float
    queue_s: float
    waiting_for_human_s: float
    review_rounds: int
    first_pass: bool | None  # None until merged
    failed_attempts: int
    human_interventions: int
    merged_before_gate: bool
    defects: int
    developer_cost_usd: Decimal
    review_cost_usd: Decimal

    @property
    def cost_usd(self) -> Decimal:
        return self.developer_cost_usd + self.review_cost_usd


def time_in_states(events: list[dict[str, Any]], now: datetime) -> dict[str, float]:
    """Seconds spent in each state, from transition events ordered by id."""
    moves = [e for e in events if e.get("to_state")]
    spent: dict[str, float] = {}
    for i, e in enumerate(moves):
        end = moves[i + 1]["occurred_at"] if i + 1 < len(moves) else now
        seconds = (end - e["occurred_at"]).total_seconds()
        spent[e["to_state"]] = spent.get(e["to_state"], 0.0) + seconds
    return spent


def task_metrics(
    task: dict[str, Any],
    events: list[dict[str, Any]],
    developer_cost: Decimal,
    review_cost: Decimal,
    now: datetime,
) -> TaskMetrics:
    spent = time_in_states(events, now)
    first_ready = next((e["occurred_at"] for e in events if e.get("to_state") == "READY"), None)
    done = next((e for e in events if e["type"] == "TASK_DONE"), None)
    merged = done is not None
    rounds = int(task["review_rounds"])

    def total(states: frozenset[str]) -> float:
        return round(sum(v for k, v in spent.items() if k in states), 3)

    interventions = sum(
        1
        for e in events
        if str(e["actor"]).startswith("human:") and e["type"] not in ROUTINE_HUMAN_EVENTS
    )
    return TaskMetrics(
        key=task["key"],
        state=task["state"],
        merged=merged,
        tags=tuple(task.get("tags") or ()),
        cycle_s=(
            round((done["occurred_at"] - first_ready).total_seconds(), 3)
            if done and first_ready
            else None
        ),
        agent_s=total(AGENT_STATES),
        review_s=total(REVIEW_STATES),
        queue_s=total(QUEUE_STATES),
        waiting_for_human_s=total(HUMAN_STATES),
        review_rounds=rounds,
        first_pass=(rounds == 0) if merged else None,
        failed_attempts=sum(1 for e in events if e["type"] in FAILURE_EVENTS),
        human_interventions=interventions,
        merged_before_gate=bool(done and (done.get("payload") or {}).get("merged_before_gate")),
        defects=sum(1 for e in events if e["type"] == "DEFECT_RECORDED"),
        developer_cost_usd=developer_cost,
        review_cost_usd=review_cost,
    )


@dataclass(frozen=True)
class Summary:
    tasks: int
    merged: int
    first_pass_rate: float | None
    rework_rounds_per_merged: float | None
    median_cycle_s: float | None
    human_interventions_per_merged: float | None
    median_waiting_for_human_s: float | None
    cost_per_merged_usd: Decimal | None
    total_cost_usd: Decimal
    review_share_of_cost: float | None
    defects_after_merge: int
    merged_before_gate: int
    verdicts: dict[str, int] = field(default_factory=dict)


def summarize(metrics: list[TaskMetrics], verdicts: dict[str, int] | None = None) -> Summary:
    merged = [m for m in metrics if m.merged]
    n = len(merged)
    total = sum((m.cost_usd for m in metrics), Decimal("0"))
    review = sum((m.review_cost_usd for m in metrics), Decimal("0"))
    return Summary(
        tasks=len(metrics),
        merged=n,
        first_pass_rate=round(sum(1 for m in merged if m.first_pass) / n, 3) if n else None,
        rework_rounds_per_merged=round(sum(m.review_rounds for m in merged) / n, 3) if n else None,
        median_cycle_s=median(m.cycle_s for m in merged if m.cycle_s is not None) if n else None,
        human_interventions_per_merged=(
            round(sum(m.human_interventions for m in merged) / n, 3) if n else None
        ),
        median_waiting_for_human_s=median(m.waiting_for_human_s for m in merged) if n else None,
        # All spend, including failed and unfinished tasks, per merged task: what delivery costs.
        cost_per_merged_usd=(total / n).quantize(Decimal("0.0001")) if n else None,
        total_cost_usd=total,
        review_share_of_cost=round(float(review / total), 3) if total else None,
        defects_after_merge=sum(m.defects for m in merged),
        merged_before_gate=sum(1 for m in merged if m.merged_before_gate),
        verdicts=verdicts or {},
    )
