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
MACHINE_STATES = AGENT_STATES | REVIEW_STATES


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
    security_findings: int = 0  # reported by security reviews that ran (not carried forward)
    security_blocking: int = 0  # of which high or critical

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


def state_intervals(
    events: list[dict[str, Any]], states: frozenset[str], now: datetime
) -> list[tuple[datetime, datetime]]:
    """When the task was in any of `states`: (entered, left) pairs, from transition events."""
    moves = [e for e in events if e.get("to_state")]
    out = []
    for i, e in enumerate(moves):
        if e["to_state"] in states:
            end = moves[i + 1]["occurred_at"] if i + 1 < len(moves) else now
            out.append((e["occurred_at"], end))
    return out


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Length of time covered by at least one interval: overlapping work counted once."""
    total, cur_start, cur_end = 0.0, None, None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None and cur_start is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None and cur_start is not None:
        total += (cur_end - cur_start).total_seconds()
    return round(total, 3)


def task_metrics(
    task: dict[str, Any],
    events: list[dict[str, Any]],
    developer_cost: Decimal,
    review_cost: Decimal,
    now: datetime,
    security: tuple[int, int] = (0, 0),
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
        security_findings=security[0],
        security_blocking=security[1],
    )


@dataclass(frozen=True)
class PlanDelivery:
    """SPEC section 2 at the level of a specification: how long from the spec to merged work, and
    whether running tasks in parallel saved time."""

    plan_id: int
    tasks: int
    merged: int
    spec_to_first_pr_s: float | None  # plan requested -> first task submitted
    spec_to_last_merge_s: float | None  # plan requested -> last task merged; None until all ended
    machine_s_in_sequence: float  # agents' and reviewers' time, task by task, added up
    machine_s_elapsed: float  # the same, overlapping time counted once
    first_pass_rate: float | None
    defects_after_merge: int

    @property
    def saved_by_parallel_s(self) -> float:
        return round(self.machine_s_in_sequence - self.machine_s_elapsed, 3)


def plan_delivery(
    plan_id: int,
    requested_at: datetime,
    tasks: list[TaskMetrics],
    task_events: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> PlanDelivery:
    merged = [t for t in tasks if t.merged]
    ended = all(t.state in ("DONE", "CANCELLED") for t in tasks) and bool(merged)
    events = [e for t in tasks for e in task_events.get(t.key, [])]
    submitted = [e["occurred_at"] for e in events if e["type"] == "TASK_SUBMITTED"]
    done = [e["occurred_at"] for e in events if e["type"] == "TASK_DONE"]
    intervals = [
        iv for t in tasks for iv in state_intervals(task_events.get(t.key, []), MACHINE_STATES, now)
    ]
    n = len(merged)
    return PlanDelivery(
        plan_id=plan_id,
        tasks=len(tasks),
        merged=n,
        spec_to_first_pr_s=(
            round((min(submitted) - requested_at).total_seconds(), 3) if submitted else None
        ),
        spec_to_last_merge_s=(
            round((max(done) - requested_at).total_seconds(), 3) if ended and done else None
        ),
        machine_s_in_sequence=round(sum((b - a).total_seconds() for a, b in intervals), 3),
        machine_s_elapsed=union_seconds(intervals),
        first_pass_rate=round(sum(1 for t in merged if t.first_pass) / n, 3) if n else None,
        defects_after_merge=sum(t.defects for t in merged),
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
    security_findings_per_merged: float | None = None
    security_blocking_per_merged: float | None = None


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
        security_findings_per_merged=(
            round(sum(m.security_findings for m in merged) / n, 3) if n else None
        ),
        security_blocking_per_merged=(
            round(sum(m.security_blocking for m in merged) / n, 3) if n else None
        ),
    )
