"""T5: exclusive claims under contention, concurrency caps, heartbeats and the reaper."""

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import (
    AssignmentRejected,
    Forbidden,
    LeaseExpired,
    NoActiveExecution,
    NotExecutionOwner,
    NotFound,
)
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.leases import expire_leases, heartbeat
from axiom.orchestrator.reaper import Reaper
from axiom.orchestrator.tasks import get_task
from axiom.registry.agents import register_agent
from axiom.state.db import Conn, connect, create_pool
from tests.integration.helpers import (
    DEV1,
    DEV2,
    HUMAN,
    do,
    drive_to,
    events_for,
    expire_lease,
    new_task,
    snapshot,
)

C, S = TaskCommand, TaskState


def _lease(db: Conn, key: str) -> Any:
    row = db.execute(
        """SELECT e.lease_expires_at FROM executions e JOIN tasks t ON t.id = e.task_id
           WHERE t.key = %s AND e.status IN ('STARTING', 'RUNNING')""",
        (key,),
    ).fetchone()
    return None if row is None else row["lease_expires_at"]


def _run_concurrently(db_url: str, fns: list[Callable[[Conn], Any]]) -> list[Any]:
    """Run each fn on its own connection, all released at the same instant."""
    barrier = threading.Barrier(len(fns))
    results: list[Any] = [None] * len(fns)

    def run(i: int) -> None:
        with connect(db_url) as c:
            barrier.wait()
            try:
                results[i] = fns[i](c)
            except Exception as e:
                results[i] = e

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---- exclusive claims ----------------------------------------------------------------------


@pytest.mark.parametrize("_round", range(3))
def test_ten_agents_racing_for_three_tasks_never_double_claim(
    org: Conn, db_url: str, _round: int
) -> None:
    for i in range(2, 11):
        register_agent(org, HUMAN, key=f"QA-{i:02d}", role="qa", runtime="claude_code")
    tasks = {new_task(org, assigned_role="qa").key for _ in range(3)}
    agents = [Actor(ActorKind.AGENT, f"QA-{i:02d}") for i in range(1, 11)]

    results = _run_concurrently(db_url, [lambda c, a=a: claim_next(c, a) for a in agents])

    assert not [r for r in results if isinstance(r, Exception)]
    claimed = [r.key for r in results if r is not None]
    assert sorted(claimed) == sorted(tasks)  # each task exactly once
    assert results.count(None) == 7
    executions = org.execute("SELECT task_id, agent_id FROM executions").fetchall()
    assert len(executions) == 3
    assert len({e["agent_id"] for e in executions}) == 3


def test_claim_order_is_priority_then_age(org: Conn) -> None:
    new_task(org, priority="P3")
    high_old = new_task(org, priority="P1")
    new_task(org, priority="P1")
    task = claim_next(org, DEV1)
    assert task is not None and task.key == high_old.key


def test_claim_filters_by_project(org: Conn) -> None:
    from axiom.registry.projects import create_project

    create_project(org, HUMAN, key="OTHER", name="Other")
    new_task(org)
    other = new_task(org, project_key="OTHER")
    task = claim_next(org, DEV1, project_key="OTHER")
    assert task is not None and task.key == other.key


# ---- capacity -------------------------------------------------------------------------------


def test_agent_cannot_exceed_its_own_capacity(org: Conn) -> None:
    new_task(org)
    second = new_task(org)
    claim_next(org, DEV1)
    before = snapshot(org, second.key)
    with pytest.raises(AssignmentRejected, match="at capacity"):
        claim_next(org, DEV1)
    with pytest.raises(AssignmentRejected, match="at capacity"):
        do(org, second.key, C.ASSIGN, agent_key="DEV-01")
    assert snapshot(org, second.key) == before


def test_global_developer_cap(org: Conn) -> None:
    org.execute("UPDATE agents SET max_concurrent = 5 WHERE key = 'DEV-01'")
    keys = [new_task(org).key for _ in range(3)]
    claim_next(org, DEV1)
    claim_next(org, DEV2)
    before = snapshot(org, keys[2])
    with pytest.raises(AssignmentRejected, match="global cap reached: 2"):
        claim_next(org, DEV1)
    assert snapshot(org, keys[2]) == before


def test_global_cap_frees_up_when_a_developer_finishes(org: Conn) -> None:
    org.execute("UPDATE agents SET max_concurrent = 5 WHERE key = 'DEV-01'")
    first, second, third = (new_task(org).key for _ in range(3))
    do(org, first, C.ASSIGN)
    do(org, second, C.ASSIGN, agent_key="DEV-02")
    with pytest.raises(AssignmentRejected, match="global cap"):
        do(org, third, C.ASSIGN)
    do(org, first, C.START)
    do(org, first, C.SUBMIT)  # execution completes -> a developer slot frees up
    assert do(org, third, C.ASSIGN).state == S.ASSIGNED


def test_global_cap_does_not_limit_other_roles(org: Conn) -> None:
    org.execute("UPDATE agents SET max_concurrent = 5 WHERE key = 'QA-01'")
    for _ in range(3):
        new_task(org, assigned_role="qa")
    qa = Actor(ActorKind.AGENT, "QA-01")
    assert all(claim_next(org, qa) is not None for _ in range(3))


@pytest.mark.parametrize("_round", range(3))
def test_global_developer_cap_holds_under_contention(org: Conn, db_url: str, _round: int) -> None:
    for i in range(3, 6):
        register_agent(org, HUMAN, key=f"DEV-{i:02d}", role="developer", runtime="claude_code")
    for _ in range(5):
        new_task(org)
    agents = [Actor(ActorKind.AGENT, f"DEV-{i:02d}") for i in range(1, 6)]

    results = _run_concurrently(db_url, [lambda c, a=a: claim_next(c, a) for a in agents])

    assert len([r for r in results if r is not None and not isinstance(r, Exception)]) == 2
    rejected = [r for r in results if isinstance(r, Exception)]
    assert len(rejected) == 3 and all(isinstance(r, AssignmentRejected) for r in rejected)


def test_same_agent_claiming_concurrently_gets_one_task(org: Conn, db_url: str) -> None:
    new_task(org)
    new_task(org)
    results = _run_concurrently(db_url, [lambda c: claim_next(c, DEV1)] * 2)
    assert len([r for r in results if r is not None and not isinstance(r, Exception)]) == 1
    assert len([r for r in results if isinstance(r, AssignmentRejected)]) == 1


# ---- heartbeats ----------------------------------------------------------------------------


def test_heartbeat_extends_lease_without_writing_an_event(org: Conn) -> None:
    key = new_task(org).key
    do(org, key, C.ASSIGN, lease_seconds=30)
    before_lease, n_events = _lease(org, key), len(events_for(org, key))
    new_expiry = heartbeat(org, DEV1, key, lease_seconds=600)
    assert new_expiry > before_lease
    assert _lease(org, key) == new_expiry
    assert len(events_for(org, key)) == n_events


def test_heartbeat_by_another_agent_rejected(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    lease = _lease(org, key)
    with pytest.raises(NotExecutionOwner):
        heartbeat(org, DEV2, key)
    assert _lease(org, key) == lease


def test_heartbeat_after_expiry_rejected(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    expire_lease(org, key)
    lease = _lease(org, key)
    with pytest.raises(LeaseExpired):
        heartbeat(org, DEV1, key)
    assert _lease(org, key) == lease


@pytest.mark.parametrize("state", [S.READY, S.IN_REVIEW, S.DONE], ids=str)
def test_heartbeat_requires_an_active_execution(org: Conn, state: TaskState) -> None:
    key = drive_to(org, state)
    with pytest.raises(NoActiveExecution):
        heartbeat(org, DEV1, key)


def test_heartbeat_only_from_agents_and_known_tasks(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    with pytest.raises(Forbidden):
        heartbeat(org, HUMAN, key)
    with pytest.raises(NotFound):
        heartbeat(org, DEV1, "AX-999")


# ---- reaper --------------------------------------------------------------------------------


def test_reaper_times_out_only_expired_leases(org: Conn) -> None:
    expired = drive_to(org, S.IN_PROGRESS)
    org.execute("UPDATE agents SET max_concurrent = 2 WHERE key = 'DEV-01'")
    live = drive_to(org, S.ASSIGNED)
    expire_lease(org, expired)

    assert expire_leases(org) == [expired]

    assert get_task(org, expired).state == S.READY
    assert get_task(org, live).state == S.ASSIGNED
    event = events_for(org, expired)[-1]
    assert (event["type"], event["actor"], event["from_state"], event["to_state"]) == (
        "EXECUTION_TIMED_OUT", "system:reaper", "IN_PROGRESS", "READY",
    )  # fmt: skip


def test_reaper_is_idempotent(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    expire_lease(org, key)
    assert expire_leases(org) == [key]
    before = snapshot(org, key)
    assert expire_leases(org) == []
    assert snapshot(org, key) == before


def test_reaper_fails_task_once_retries_are_used_up(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS, max_retries=0)
    expire_lease(org, key)
    expire_leases(org)
    assert get_task(org, key).state == S.FAILED


def test_heartbeat_keeps_the_reaper_away(org: Conn) -> None:
    key = new_task(org).key
    do(org, key, C.ASSIGN, lease_seconds=1)
    time.sleep(0.6)
    heartbeat(org, DEV1, key, lease_seconds=60)
    time.sleep(0.6)  # past the original 1s lease
    assert expire_leases(org) == []
    assert get_task(org, key).state == S.ASSIGNED


def test_lease_expires_on_the_real_clock(org: Conn) -> None:
    """No simulated time: a 1-second lease without heartbeats is reaped."""
    key = new_task(org).key
    claim_next(org, DEV1, lease_seconds=1)
    assert expire_leases(org) == []
    time.sleep(1.2)
    assert expire_leases(org) == [key]
    task = get_task(org, key)
    assert (task.state, task.attempt_count) == (S.READY, 1)


def test_reaped_task_can_be_claimed_again(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    expire_lease(org, key)
    expire_leases(org)
    task = claim_next(org, DEV2)
    assert task is not None and task.key == key and task.state == S.ASSIGNED


def test_background_reaper_thread(org: Conn, db_url: str) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    expire_lease(org, key)
    with create_pool(db_url, max_size=2) as pool:
        reaper = Reaper(pool, interval_seconds=0.05)
        reaper.start()
        try:
            deadline = time.monotonic() + 5
            while get_task(org, key).state != S.READY and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            reaper.stop()
    assert get_task(org, key).state == S.READY
