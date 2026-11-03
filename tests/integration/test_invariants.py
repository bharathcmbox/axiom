"""check_invariants must detect corruption; otherwise the per-test teardown proves nothing.

These tests corrupt state on purpose, so they use their own connection (not the `db`
fixture, whose teardown asserts the invariants hold) and clean up after themselves.
"""

from collections.abc import Iterator

import pytest

from axiom.domain.task_machine import TaskCommand
from axiom.registry.agents import seed_agents
from axiom.registry.projects import create_project
from axiom.registry.seed import DEFAULT_CONFIG
from axiom.state.db import Conn, connect
from axiom.state.invariants import check_invariants
from tests.conftest import APP_TABLES
from tests.integration.helpers import HUMAN, do, new_task


@pytest.fixture
def raw(db_url: str) -> Iterator[Conn]:
    with connect(db_url) as c:
        create_project(c, HUMAN, key="AX", name="Axiom")
        seed_agents(c, HUMAN, DEFAULT_CONFIG)
        yield c
        c.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")


def test_healthy_state_has_no_violations(raw: Conn) -> None:
    key = new_task(raw).key
    do(raw, key, TaskCommand.ASSIGN)
    assert check_invariants(raw) == []


def test_detects_state_changed_without_event(raw: Conn) -> None:
    key = new_task(raw).key
    raw.execute("UPDATE tasks SET state = 'IN_REVIEW' WHERE key = %s", (key,))
    assert any("events end at READY but task is IN_REVIEW" in v for v in check_invariants(raw))


def test_detects_executing_task_without_execution(raw: Conn) -> None:
    key = new_task(raw).key
    raw.execute("UPDATE tasks SET state = 'ASSIGNED' WHERE key = %s", (key,))
    assert any("ASSIGNED with 0 active executions" in v for v in check_invariants(raw))


def test_detects_pending_task_with_done_dependencies(raw: Conn) -> None:
    dep = new_task(raw)
    task = new_task(raw, dependencies=[dep.key])
    raw.execute("UPDATE tasks SET state = 'DONE' WHERE key = %s", (dep.key,))
    assert any(f"{task.key}: PENDING although" in v for v in check_invariants(raw))


def test_detects_failed_task_with_retries_left(raw: Conn) -> None:
    key = new_task(raw).key
    raw.execute("UPDATE tasks SET state = 'FAILED' WHERE key = %s", (key,))
    assert any("FAILED with retries left" in v for v in check_invariants(raw))


def test_detects_agent_over_capacity(raw: Conn) -> None:
    a, b = new_task(raw).key, new_task(raw).key
    do(raw, a, TaskCommand.ASSIGN)
    raw.execute("UPDATE agents SET max_concurrent = 2 WHERE key = 'DEV-01'")
    do(raw, b, TaskCommand.ASSIGN)
    raw.execute("UPDATE agents SET max_concurrent = 1 WHERE key = 'DEV-01'")
    assert any("agent DEV-01: 2 active > max_concurrent 1" in v for v in check_invariants(raw))
