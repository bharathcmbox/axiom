"""Leases: heartbeats extend them; the reaper times out executions whose lease has expired.

All lease arithmetic uses the database clock (`now()`), never the application clock.
"""

import logging
from datetime import datetime

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import IllegalTransition, TaskCommand
from axiom.errors import (
    Forbidden,
    LeaseExpired,
    LeaseNotExpired,
    NoActiveExecution,
    NotExecutionOwner,
    NotFound,
)
from axiom.orchestrator.commands import apply_locked
from axiom.state.db import Conn

log = logging.getLogger(__name__)
REAPER = Actor(ActorKind.SYSTEM, "reaper")


def heartbeat(
    conn: Conn, actor: Actor, task_key: str, *, lease_seconds: int | None = None
) -> datetime:
    """Extend the lease on the task's active execution. Returns the new expiry.

    Not a state transition, so no event is written. Only the owning agent may heartbeat,
    and only while its lease is still live; an expired lease belongs to the reaper.
    """
    if actor.kind != ActorKind.AGENT:
        raise Forbidden("only agents send heartbeats")
    lease = lease_seconds if lease_seconds is not None else get_settings().lease_seconds
    with conn.transaction():
        # Same lock order as commands: task row first, then its execution.
        task = conn.execute(
            "SELECT id FROM tasks WHERE key = %s FOR UPDATE", (task_key,)
        ).fetchone()
        if task is None:
            raise NotFound(f"task {task_key} not found")
        execution = conn.execute(
            """SELECT e.id, a.key AS agent_key, (e.lease_expires_at <= now()) AS expired
               FROM executions e JOIN agents a ON a.id = e.agent_id
               WHERE e.task_id = %s AND e.status IN ('STARTING', 'RUNNING')
               FOR UPDATE OF e""",
            (task["id"],),
        ).fetchone()
        if execution is None:
            raise NoActiveExecution(f"task {task_key} has no active execution")
        if execution["agent_key"] != actor.name:
            raise NotExecutionOwner(
                f"execution {execution['id']} belongs to {execution['agent_key']}"
            )
        if execution["expired"]:
            raise LeaseExpired(f"lease on execution {execution['id']} has expired")
        row = conn.execute(
            """UPDATE executions SET lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s RETURNING lease_expires_at""",
            (lease, execution["id"]),
        ).fetchone()
        assert row is not None
        expires: datetime = row["lease_expires_at"]
        return expires


def expire_leases(conn: Conn) -> list[str]:
    """Time out every execution whose lease has expired. Returns the affected task keys.

    Each task is handled in its own transaction. Tasks locked by someone else are skipped and
    picked up on the next sweep; tasks whose lease was renewed or that finished in the
    meantime are left alone (the command's guards re-check under the lock).
    """
    candidates = conn.execute(
        """SELECT t.key FROM executions e JOIN tasks t ON t.id = e.task_id
           WHERE e.status IN ('STARTING', 'RUNNING') AND e.lease_expires_at <= now()
           ORDER BY e.lease_expires_at, e.id"""
    ).fetchall()
    timed_out = []
    for c in candidates:
        try:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM tasks WHERE key = %s FOR UPDATE SKIP LOCKED", (c["key"],)
                ).fetchone()
                if row is None:
                    continue  # busy; next sweep
                apply_locked(
                    conn, row, TaskCommand.TIME_OUT, REAPER, payload={"reason": "lease expired"}
                )
                timed_out.append(c["key"])
        except IllegalTransition, LeaseNotExpired:
            continue  # state moved on since the candidate query
    if timed_out:
        log.info("timed out %d execution(s): %s", len(timed_out), ", ".join(timed_out))
    return timed_out
