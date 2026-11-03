"""Pull-based work assignment: an agent claims the next READY task for its role."""

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import ActorNotPermitted, TaskCommand
from axiom.orchestrator.commands import apply_locked
from axiom.orchestrator.tasks import Task, get_task
from axiom.registry.agents import get_agent
from axiom.state.db import Conn


def claim_next(
    conn: Conn,
    actor: Actor,
    *,
    project_key: str | None = None,
    lease_seconds: int | None = None,
) -> Task | None:
    """Claim the highest-priority, oldest READY task for the agent's role, or return None.

    `FOR UPDATE SKIP LOCKED` lets concurrent claimers pass over rows another transaction is
    claiming, so two agents never receive the same task and never block each other.
    """
    if actor.kind != ActorKind.AGENT:
        raise ActorNotPermitted(TaskCommand.CLAIM, actor.kind)
    agent = get_agent(conn, actor.name)
    with conn.transaction():
        row = conn.execute(
            """SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id
               WHERE t.state = 'READY' AND t.assigned_role = %(role)s
                 AND (%(project)s::text IS NULL OR p.key = %(project)s)
               ORDER BY t.priority, t.created_at, t.id
               LIMIT 1
               FOR UPDATE OF t SKIP LOCKED""",
            {"role": agent.role, "project": project_key},
        ).fetchone()
        if row is None:
            return None
        apply_locked(conn, row, TaskCommand.CLAIM, actor, payload={}, lease_seconds=lease_seconds)
        return get_task(conn, row["key"])
