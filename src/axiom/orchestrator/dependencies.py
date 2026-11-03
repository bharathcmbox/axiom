"""Dependency queries. Dependencies are fixed at task creation and must already exist, so the
dependency graph cannot contain cycles."""

from axiom.state.db import Conn


def dependencies_satisfied(conn: Conn, task_id: int) -> bool:
    """True when every dependency of the task is DONE (vacuously true with none)."""
    row = conn.execute(
        """SELECT NOT EXISTS (
             SELECT 1 FROM task_dependencies td
             JOIN tasks d ON d.id = td.depends_on_id
             WHERE td.task_id = %s AND d.state <> 'DONE'
           ) AS ok""",
        (task_id,),
    ).fetchone()
    assert row is not None
    return bool(row["ok"])


def pending_dependents(conn: Conn, task_id: int) -> list[int]:
    """Ids of PENDING tasks that depend on `task_id`, in id order (stable lock order)."""
    rows = conn.execute(
        """SELECT td.task_id FROM task_dependencies td
           JOIN tasks t ON t.id = td.task_id
           WHERE td.depends_on_id = %s AND t.state = 'PENDING'
           ORDER BY td.task_id""",
        (task_id,),
    )
    return [r["task_id"] for r in rows]
