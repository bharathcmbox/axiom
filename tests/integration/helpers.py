"""Helpers that drive tasks through real commands (never by writing state directly)."""

from typing import Any

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.commands import apply_command
from axiom.orchestrator.tasks import Task, create_task, get_task
from axiom.state.db import Conn

HUMAN = Actor(ActorKind.HUMAN, "tester")
DEV1 = Actor(ActorKind.AGENT, "DEV-01")
DEV2 = Actor(ActorKind.AGENT, "DEV-02")
REAPER = Actor(ActorKind.SYSTEM, "reaper")
SYSTEM_DEPS = Actor(ActorKind.SYSTEM, "dependencies")

C = TaskCommand
DEFAULT_ACTOR: dict[TaskCommand, Actor] = {
    C.PROMOTE: SYSTEM_DEPS,
    C.CLAIM: DEV1,
    C.ASSIGN: HUMAN,
    C.START: DEV1,
    C.SUBMIT: DEV1,
    C.ASK_HUMAN: DEV1,
    C.ANSWER: HUMAN,
    C.FAIL: DEV1,
    C.TIME_OUT: REAPER,
    C.APPROVE: HUMAN,
    C.REQUEST_CHANGES: HUMAN,
    C.REJECT_MERGE: HUMAN,
    C.MERGE: HUMAN,
    C.RETRY: HUMAN,
    C.CANCEL: HUMAN,
}
DEFAULT_PAYLOAD: dict[TaskCommand, dict[str, Any]] = {
    C.SUBMIT: {"summary": "done", "files_changed": ["a.py"]},
    C.ASK_HUMAN: {"question": "Which database?"},
    C.ANSWER: {"answer": "Postgres"},
    C.FAIL: {"error": "tests failed"},
    C.REQUEST_CHANGES: {"findings": [{"severity": "high", "issue": "missing test"}]},
}

S = TaskState
PATH: dict[TaskState, list[TaskCommand]] = {
    S.READY: [],
    S.ASSIGNED: [C.ASSIGN],
    S.IN_PROGRESS: [C.ASSIGN, C.START],
    S.WAITING_FOR_HUMAN: [C.ASSIGN, C.START, C.ASK_HUMAN],
    S.IN_REVIEW: [C.ASSIGN, C.START, C.SUBMIT],
    S.AWAITING_MERGE: [C.ASSIGN, C.START, C.SUBMIT, C.APPROVE],
    S.DONE: [C.ASSIGN, C.START, C.SUBMIT, C.APPROVE, C.MERGE],
    S.FAILED: [C.ASSIGN, C.START, C.FAIL],  # with max_retries=0
    S.CANCELLED: [C.CANCEL],
}


def new_task(db: Conn, **kwargs: Any) -> Task:
    args: dict[str, Any] = {"project_key": "AX", "title": "t", "specification": "s"} | kwargs
    return create_task(db, HUMAN, **args)


def expire_lease(db: Conn, task_key: str) -> None:
    """Simulate time passing: move a live lease into the past (no-op if already expired)."""
    db.execute(
        """UPDATE executions SET lease_expires_at = now() - interval '1 second'
           WHERE status IN ('STARTING', 'RUNNING') AND lease_expires_at > now()
             AND task_id = (SELECT id FROM tasks WHERE key = %s)""",
        (task_key,),
    )


def do(db: Conn, key: str, command: TaskCommand, actor: Actor | None = None, **kwargs: Any) -> Task:
    """Issue a command with sensible defaults for actor, agent and payload."""
    actor = actor or DEFAULT_ACTOR[command]
    if command == C.CLAIM:
        task = claim_next(db, actor)
        assert task is not None and task.key == key
        return task
    if command == C.TIME_OUT and get_task(db, key).state in (S.ASSIGNED, S.IN_PROGRESS):
        expire_lease(db, key)
    if command == C.ASSIGN:
        kwargs.setdefault("agent_key", "DEV-01")
    kwargs.setdefault("payload", DEFAULT_PAYLOAD.get(command))
    return apply_command(db, actor, key, command, **kwargs)


def drive_to(db: Conn, state: TaskState, *, max_retries: int | None = None) -> str:
    """Create a task and move it to `state` using only legal commands. Returns its key."""
    if state == S.PENDING:
        dep = new_task(db, title="dependency")
        return new_task(db, dependencies=[dep.key]).key
    if max_retries is None:
        max_retries = 0 if state == S.FAILED else 2
    key = new_task(db, max_retries=max_retries).key
    for command in PATH[state]:
        do(db, key, command)
    assert get_task(db, key).state == state
    return key


def snapshot(db: Conn, key: str) -> dict[str, Any]:
    """Everything a command could change: the task, its executions, and the event count."""
    task = db.execute("SELECT * FROM tasks WHERE key = %s", (key,)).fetchone()
    assert task is not None
    executions = db.execute(
        "SELECT * FROM executions WHERE task_id = %s ORDER BY id", (task["id"],)
    ).fetchall()
    n_events = db.execute("SELECT count(*) AS n FROM events").fetchone()
    return {"task": task, "executions": executions, "events": n_events}


def events_for(db: Conn, key: str) -> list[dict[str, Any]]:
    return db.execute(
        """SELECT e.* FROM events e JOIN tasks t ON t.id = e.task_id
           WHERE t.key = %s ORDER BY e.id""",
        (key,),
    ).fetchall()
