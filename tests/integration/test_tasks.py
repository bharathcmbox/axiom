"""T4: task creation, dependencies, retries, rework counters, and stored payloads."""

import threading
from decimal import Decimal

import pytest

from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.orchestrator.tasks import create_task, get_task, list_tasks
from axiom.registry.projects import create_project
from axiom.state.db import Conn, connect
from tests.integration.helpers import DEV1, HUMAN, do, drive_to, events_for, new_task

C, S = TaskCommand, TaskState


# ---- creation -----------------------------------------------------------------------------


def test_task_without_dependencies_is_created_ready(org: Conn) -> None:
    task = new_task(
        org,
        title="Implement POST /alerts",
        acceptance_criteria=["rejects malformed payloads"],
        priority="P1",
        budget_usd=Decimal("3.00"),
    )
    assert (task.key, task.state, task.priority, task.budget_usd) == (
        "AX-001", S.READY, "P1", Decimal("3.00"),
    )  # fmt: skip
    assert [
        (e["type"], e["from_state"], e["to_state"], e["actor"]) for e in events_for(org, task.key)
    ] == [
        ("TASK_CREATED", None, "PENDING", "human:tester"),
        ("TASK_READY", "PENDING", "READY", "system:dependencies"),
    ]


def test_task_keys_are_sequential_per_project(org: Conn) -> None:
    create_project(org, HUMAN, key="OTHER", name="Other")
    assert new_task(org).key == "AX-001"
    assert new_task(org).key == "AX-002"
    assert new_task(org, project_key="OTHER").key == "OTHER-001"
    assert [t.key for t in list_tasks(org, project_key="AX")] == ["AX-001", "AX-002"]
    assert [t.key for t in list_tasks(org, state="READY")] == ["AX-001", "AX-002", "OTHER-001"]


def test_only_humans_create_tasks(org: Conn) -> None:
    with pytest.raises(Forbidden):
        create_task(org, DEV1, project_key="AX", title="t", specification="s")


@pytest.mark.parametrize(
    "bad",
    [
        {"title": " "},
        {"specification": ""},
        {"acceptance_criteria": [""]},
        {"assigned_role": "cto"},
        {"priority": "P9"},
        {"max_retries": -1},
        {"budget_usd": Decimal("-1")},
        {"dependencies": ["AX-999"]},
    ],
)
def test_invalid_tasks_rejected_without_side_effects(org: Conn, bad: dict[str, object]) -> None:
    with pytest.raises(InvalidInput):
        new_task(org, **bad)
    assert list_tasks(org) == []
    assert new_task(org).key == "AX-001"  # sequence not consumed by the failed attempt


def test_unknown_project(org: Conn) -> None:
    with pytest.raises(NotFound):
        new_task(org, project_key="NOPE")


def test_dependency_must_be_in_same_project(org: Conn) -> None:
    create_project(org, HUMAN, key="OTHER", name="Other")
    other = new_task(org, project_key="OTHER")
    with pytest.raises(InvalidInput, match="must be in project"):
        new_task(org, dependencies=[other.key])


# ---- dependencies -------------------------------------------------------------------------


def _merge(db: Conn, key: str) -> None:
    for command in (C.ASSIGN, C.START, C.SUBMIT, C.APPROVE, C.MERGE):
        do(db, key, command)


def test_task_with_unfinished_dependency_stays_pending(org: Conn) -> None:
    dep = new_task(org)
    task = new_task(org, dependencies=[dep.key])
    assert task.state == S.PENDING
    assert task.dependencies == (dep.key,)


def test_merging_last_dependency_promotes_dependent(org: Conn) -> None:
    a, b = new_task(org), new_task(org)
    task = new_task(org, dependencies=[a.key, b.key])
    _merge(org, a.key)
    assert get_task(org, task.key).state == S.PENDING  # one of two done
    _merge(org, b.key)
    assert get_task(org, task.key).state == S.READY
    promote = events_for(org, task.key)[-1]
    assert (promote["type"], promote["actor"]) == ("TASK_READY", "system:dependencies")


def test_dependency_already_done_at_creation(org: Conn) -> None:
    dep = new_task(org)
    _merge(org, dep.key)
    assert new_task(org, dependencies=[dep.key]).state == S.READY


def test_cancelled_dependency_keeps_dependent_pending(org: Conn) -> None:
    dep = new_task(org)
    task = new_task(org, dependencies=[dep.key])
    do(org, dep.key, C.CANCEL)
    assert get_task(org, task.key).state == S.PENDING  # a human must cancel or re-plan it


def test_chain_of_dependencies_promotes_one_level_at_a_time(org: Conn) -> None:
    a = new_task(org)
    b = new_task(org, dependencies=[a.key])
    c = new_task(org, dependencies=[b.key])
    _merge(org, a.key)
    assert (get_task(org, b.key).state, get_task(org, c.key).state) == (S.READY, S.PENDING)
    _merge(org, b.key)
    assert get_task(org, c.key).state == S.READY


@pytest.mark.parametrize("_round", range(5))
def test_concurrent_merges_of_two_dependencies_promote_dependent(
    org: Conn, db_url: str, _round: int
) -> None:
    """Both dependencies merged at the same moment: the dependent must not be left PENDING."""
    a, b = new_task(org), new_task(org)
    task = new_task(org, dependencies=[a.key, b.key])
    for key in (a.key, b.key):
        for command in (C.ASSIGN, C.START, C.SUBMIT, C.APPROVE):
            do(org, key, command)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def merge(key: str) -> None:
        try:
            with connect(db_url) as c:
                barrier.wait()
                do(c, key, C.MERGE)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=merge, args=(k,)) for k in (a.key, b.key)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert get_task(org, task.key).state == S.READY


# ---- retries, rework and payloads -----------------------------------------------------------


def test_fail_retries_then_fails_after_max_retries(org: Conn) -> None:
    key = new_task(org).key  # max_retries = 2 -> 3 attempts
    for attempt in (1, 2):
        do(org, key, C.ASSIGN)
        do(org, key, C.START)
        task = do(org, key, C.FAIL)
        assert (task.state, task.attempt_count) == (S.READY, attempt)
    do(org, key, C.ASSIGN)
    do(org, key, C.START)
    task = do(org, key, C.FAIL)
    assert (task.state, task.attempt_count) == (S.FAILED, 3)

    task = do(org, key, C.RETRY)
    assert (task.state, task.attempt_count) == (S.READY, 0)


def test_time_out_counts_as_an_attempt(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS, max_retries=1)
    task = do(org, key, C.TIME_OUT)
    assert (task.state, task.attempt_count) == (S.READY, 1)
    do(org, key, C.ASSIGN)
    task = do(org, key, C.TIME_OUT)  # timed out before even starting
    assert (task.state, task.attempt_count) == (S.FAILED, 2)
    statuses = org.execute(
        "SELECT status, error FROM executions WHERE task_id = %s ORDER BY id", (task.id,)
    ).fetchall()
    assert statuses == [
        {"status": "TIMED_OUT", "error": "lease expired"},
        {"status": "TIMED_OUT", "error": "lease expired"},
    ]


def test_rework_counts_review_rounds_and_numbers_executions(org: Conn) -> None:
    key = drive_to(org, S.IN_REVIEW)
    task = do(org, key, C.REQUEST_CHANGES)
    assert (task.state, task.review_rounds) == (S.READY, 1)
    for command in (C.ASSIGN, C.START, C.SUBMIT, C.APPROVE):
        do(org, key, command)
    task = do(org, key, C.REJECT_MERGE)
    assert (task.state, task.review_rounds) == (S.READY, 2)
    assert task.attempt_count == 0  # rework is not a failure
    attempts = org.execute(
        "SELECT attempt FROM executions WHERE task_id = %s ORDER BY id", (task.id,)
    ).fetchall()
    assert [r["attempt"] for r in attempts] == [1, 2]


def test_payloads_are_stored_on_executions_and_events(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    do(org, key, C.SUBMIT, payload={"summary": "implemented", "cost_usd": 1.2})
    task = get_task(org, key)
    execution = org.execute(
        "SELECT status, result, ended_at, lease_expires_at FROM executions WHERE task_id = %s",
        (task.id,),
    ).fetchone()
    assert execution is not None
    assert execution["status"] == "COMPLETED"
    assert execution["result"] == {"summary": "implemented", "cost_usd": 1.2}
    assert execution["ended_at"] is not None and execution["lease_expires_at"] is None
    do(org, key, C.REQUEST_CHANGES, payload={"findings": ["x"]})
    [reopened] = [e for e in events_for(org, key) if e["type"] == "TASK_REOPENED"]
    assert reopened["payload"]["findings"] == ["x"]


def test_ask_human_and_answer(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    do(org, key, C.ASK_HUMAN, payload={"question": "REST or gRPC?"})
    task = do(org, key, C.ANSWER, payload={"answer": "REST"})
    assert task.state == S.READY
    types = [e["type"] for e in events_for(org, key)][-2:]
    assert types == ["HUMAN_INPUT_REQUESTED", "HUMAN_INPUT_PROVIDED"]


def test_cancel_cancels_the_active_execution(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    task = do(org, key, C.CANCEL)
    row = org.execute("SELECT status FROM executions WHERE task_id = %s", (task.id,)).fetchone()
    assert row == {"status": "CANCELLED"}


def test_full_happy_path_event_history(org: Conn) -> None:
    key = new_task(org).key
    do(org, key, C.CLAIM)
    for command in (C.START, C.SUBMIT, C.APPROVE, C.MERGE):
        do(org, key, command)
    assert [e["type"] for e in events_for(org, key)] == [
        "TASK_CREATED", "TASK_READY", "TASK_ASSIGNED", "AGENT_STARTED",
        "TASK_SUBMITTED", "REVIEW_REQUESTED", "REVIEW_REQUESTED",
        "REVIEW_APPROVED", "REVIEW_SUPERSEDED", "REVIEW_SUPERSEDED", "TASK_DONE",
    ]  # fmt: skip
