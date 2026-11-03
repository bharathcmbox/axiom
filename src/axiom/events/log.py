"""The append-only event log. This is the only module that writes to `events`."""

from typing import Any

from psycopg.types.json import Jsonb

from axiom.domain.roles import Actor
from axiom.state.db import Conn


def append_event(
    conn: Conn,
    *,
    actor: Actor,
    type: str,
    project_id: int | None = None,
    task_id: int | None = None,
    execution_id: int | None = None,
    agent_id: int | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert one event and return its id. Call inside the transaction that made the change."""
    row = conn.execute(
        """INSERT INTO events (actor, type, project_id, task_id, execution_id, agent_id,
                               from_state, to_state, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            str(actor),
            type,
            project_id,
            task_id,
            execution_id,
            agent_id,
            from_state,
            to_state,
            Jsonb(payload or {}),
        ),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def list_events(
    conn: Conn, *, task_id: int | None = None, after_id: int = 0, limit: int = 1000
) -> list[dict[str, Any]]:
    if task_id is None:
        return conn.execute(
            "SELECT * FROM events WHERE id > %s ORDER BY id LIMIT %s", (after_id, limit)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM events WHERE task_id = %s AND id > %s ORDER BY id LIMIT %s",
        (task_id, after_id, limit),
    ).fetchall()
