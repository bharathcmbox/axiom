"""Phase 4 T1: each metric's definition, on scripted histories (no database)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from axiom.metrics import summarize, task_metrics, time_in_states

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ev(
    minute: float, type_: str, to: str | None = None, actor: str = "system:x", **p: Any
) -> dict[str, Any]:
    return {"occurred_at": at(minute), "type": type_, "to_state": to, "actor": actor, "payload": p}


HISTORY = [
    ev(0, "TASK_CREATED", "PENDING", "human:b"),
    ev(0, "TASK_READY", "READY"),
    ev(5, "TASK_ASSIGNED", "ASSIGNED", "agent:DEV-01"),
    ev(5, "AGENT_STARTED", "IN_PROGRESS", "agent:DEV-01"),
    ev(15, "TASK_SUBMITTED", "IN_REVIEW", "agent:DEV-01"),
    ev(15, "REVIEW_REQUESTED"),  # review bookkeeping must not count as a state change
    ev(20, "TASK_REOPENED", "READY", "system:aggregator"),
    ev(21, "TASK_ASSIGNED", "ASSIGNED", "agent:DEV-02"),
    ev(21, "AGENT_STARTED", "IN_PROGRESS", "agent:DEV-02"),
    ev(31, "TASK_SUBMITTED", "IN_REVIEW", "agent:DEV-02"),
    ev(36, "REVIEW_APPROVED", "AWAITING_MERGE", "system:aggregator"),
    ev(96, "TASK_DONE", "DONE", "human:github-b", merged_before_gate=False),
]
TASK = {"key": "A-1", "state": "DONE", "review_rounds": 1, "tags": []}


def _m(
    events: list[dict[str, Any]] = HISTORY,
    task: dict[str, Any] = TASK,
    dev: str = "1.00",
    rev: str = "0.50",
) -> Any:
    return task_metrics(task, events, Decimal(dev), Decimal(rev), at(1000))


def test_time_in_states_ignores_non_transition_events() -> None:
    spent = time_in_states(HISTORY, at(1000))
    assert spent["IN_REVIEW"] == 10 * 60  # 15-20 and 31-36
    assert spent["AWAITING_MERGE"] == 60 * 60
    assert spent["DONE"] == (1000 - 96) * 60  # still in the final state at `now`


def test_task_durations_add_up() -> None:
    m = _m()
    assert m.cycle_s == 96 * 60
    assert m.queue_s == (5 + 1) * 60
    assert m.agent_s == (10 + 10) * 60
    assert m.review_s == 10 * 60
    assert m.waiting_for_human_s == 60 * 60
    assert m.queue_s + m.agent_s + m.review_s + m.waiting_for_human_s == m.cycle_s


def test_rework_is_not_first_pass_and_costs_add_up() -> None:
    m = _m()
    assert m.merged and m.first_pass is False and m.review_rounds == 1
    assert m.cost_usd == Decimal("1.50")


def test_first_pass_is_only_known_after_merge() -> None:
    unfinished = _m(
        HISTORY[:5], {"key": "A-2", "state": "IN_REVIEW", "review_rounds": 0, "tags": []}
    )
    assert (
        unfinished.merged is False and unfinished.first_pass is None and unfinished.cycle_s is None
    )
    clean = _m(HISTORY, {"key": "A-3", "state": "DONE", "review_rounds": 0, "tags": []})
    assert clean.first_pass is True


def test_human_interventions_exclude_the_normal_flow() -> None:
    assert _m().human_interventions == 0  # creating and merging are routine
    extra = [*HISTORY, ev(97, "TASK_RETRIED", "READY", "human:b"), ev(98, "HUMAN_INPUT_PROVIDED", "READY", "human:b"),
             ev(99, "TASK_TAGGED", None, "human:b"), ev(99, "DEFECT_RECORDED", None, "human:b")]  # fmt: skip
    m = _m(extra)
    assert m.human_interventions == 2 and m.defects == 1


def test_failed_attempts_and_early_merges() -> None:
    events = [ev(0, "TASK_READY", "READY"), ev(1, "EXECUTION_FAILED", "READY"),
              ev(2, "EXECUTION_TIMED_OUT", "READY"),
              ev(3, "TASK_DONE", "DONE", "human:g", merged_before_gate=True)]  # fmt: skip
    m = _m(events)
    assert m.failed_attempts == 2 and m.merged_before_gate is True


def test_summary_counts_all_spend_per_merged_task() -> None:
    merged = _m()
    failed = _m(
        HISTORY[:5], {"key": "A-2", "state": "FAILED", "review_rounds": 0, "tags": []}, "2.00", "0"
    )
    s = summarize([merged, failed], {"PASS": 2, "FAIL": 1})
    assert (s.tasks, s.merged) == (2, 1)
    assert s.total_cost_usd == Decimal("3.50")
    assert s.cost_per_merged_usd == Decimal("3.5000")  # the failed task's spend still counts
    assert s.review_share_of_cost == pytest.approx(0.143)
    assert s.first_pass_rate == 0.0 and s.rework_rounds_per_merged == 1.0
    assert s.median_cycle_s == 96 * 60 and s.verdicts == {"PASS": 2, "FAIL": 1}


def test_summary_of_nothing_is_none_not_zero() -> None:
    s = summarize([])
    assert (
        s.first_pass_rate is None
        and s.cost_per_merged_usd is None
        and s.review_share_of_cost is None
    )
