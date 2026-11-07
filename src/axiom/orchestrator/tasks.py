"""Task records: creation and reads. State changes go through orchestrator.commands."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind, Role
from axiom.domain.task_machine import INITIAL_STATE, TaskState
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.orchestrator import budgets
from axiom.state.db import Conn

PRIORITIES = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class Task:
    id: int
    project_id: int
    key: str
    title: str
    specification: str
    acceptance_criteria: list[str]
    assigned_role: Role
    priority: str
    state: TaskState
    attempt_count: int
    max_retries: int
    review_rounds: int
    budget_usd: Decimal | None
    branch: str | None
    pull_request_url: str | None
    created_at: datetime
    updated_at: datetime
    allowed_paths: list[str]
    head_sha: str | None
    ci_status: str | None
    ci_sha: str | None
    plan_id: int | None
    plan_ref: str | None
    dependencies: tuple[str, ...]
    budget_spent_usd: Decimal
    run_budget_usd: Decimal


def _dependency_keys(conn: Conn, task_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT d.key FROM task_dependencies td JOIN tasks d ON d.id = td.depends_on_id
           WHERE td.task_id = %s ORDER BY d.id""",
        (task_id,),
    )
    return tuple(r["key"] for r in rows)


_COMPUTED = ("dependencies", "budget_spent_usd", "run_budget_usd")


def task_from_row(conn: Conn, row: dict[str, Any]) -> Task:
    fields = {f: row[f] for f in Task.__dataclass_fields__ if f not in _COMPUTED}
    return Task(
        **fields
        | {
            "assigned_role": Role(row["assigned_role"]),
            "state": TaskState(row["state"]),
            "dependencies": _dependency_keys(conn, row["id"]),
            "budget_spent_usd": budgets.task_spent(conn, row["id"]),
            "run_budget_usd": budgets.run_budget(conn, row["id"], row["budget_usd"]),
        }
    )


def get_task(conn: Conn, key: str) -> Task:
    row = conn.execute("SELECT * FROM tasks WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise NotFound(f"task {key} not found")
    return task_from_row(conn, row)


def list_tasks(
    conn: Conn, *, project_key: str | None = None, state: str | None = None
) -> list[Task]:
    rows = conn.execute(
        """SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id
           WHERE (%(project)s::text IS NULL OR p.key = %(project)s)
             AND (%(state)s::text IS NULL OR t.state = %(state)s)
           ORDER BY t.id""",
        {"project": project_key, "state": state},
    ).fetchall()
    return [task_from_row(conn, r) for r in rows]


def _validate(
    title: str,
    specification: str,
    acceptance_criteria: list[str],
    assigned_role: str,
    priority: str,
    max_retries: int,
    budget_usd: Decimal | None,
) -> Role:
    if not title.strip():
        raise InvalidInput("title must not be empty")
    if not specification.strip():
        raise InvalidInput("specification must not be empty")
    if not all(isinstance(c, str) and c.strip() for c in acceptance_criteria):
        raise InvalidInput("acceptance_criteria must be non-empty strings")
    try:
        role = Role(assigned_role)
    except ValueError:
        raise InvalidInput(f"unknown role {assigned_role!r}") from None
    if priority not in PRIORITIES:
        raise InvalidInput(f"priority must be one of {PRIORITIES}")
    if max_retries < 0:
        raise InvalidInput("max_retries must be >= 0")
    if budget_usd is not None and budget_usd < 0:
        raise InvalidInput("budget_usd must be >= 0")
    return role


def validate_allowed_paths(paths: list[str]) -> list[str]:
    """Repository-relative paths or glob patterns (`agent/**`); never absolute or escaping."""
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise InvalidInput("allowed_paths entries must be non-empty strings")
        if p.startswith(("/", "~")) or "\\" in p or ".." in p.split("/"):
            raise InvalidInput(f"allowed_paths must be repository-relative: {p!r}")
    return list(dict.fromkeys(paths))


def create_task(
    conn: Conn,
    actor: Actor,
    *,
    project_key: str,
    title: str,
    specification: str,
    acceptance_criteria: list[str] | None = None,
    assigned_role: str = "developer",
    priority: str = "P2",
    max_retries: int = 2,
    budget_usd: Decimal | None = None,
    dependencies: list[str] | None = None,
    allowed_paths: list[str] | None = None,
) -> Task:
    """Create a task in PENDING; promote it to READY at once if its dependencies are DONE."""
    from axiom.orchestrator.commands import promote_if_ready  # avoid import cycle

    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans create tasks")
    criteria = acceptance_criteria or []
    role = _validate(
        title, specification, criteria, assigned_role, priority, max_retries, budget_usd
    )
    dep_keys = list(dict.fromkeys(dependencies or []))  # dedupe, keep order
    paths = validate_allowed_paths(allowed_paths or [])
    if budget_usd is None:
        budget_usd = get_settings().default_task_budget_usd

    with conn.transaction():
        project = conn.execute(
            """UPDATE projects SET task_seq = task_seq + 1 WHERE key = %s
               RETURNING id, task_seq""",
            (project_key,),
        ).fetchone()
        if project is None:
            raise NotFound(f"project {project_key} not found")
        key = f"{project_key}-{project['task_seq']:03d}"

        deps = conn.execute(
            "SELECT id, key, project_id FROM tasks WHERE key = ANY(%s)", (dep_keys,)
        ).fetchall()
        missing = set(dep_keys) - {d["key"] for d in deps}
        if missing:
            raise InvalidInput(f"unknown dependencies: {sorted(missing)}")
        foreign = [d["key"] for d in deps if d["project_id"] != project["id"]]
        if foreign:
            raise InvalidInput(f"dependencies must be in project {project_key}: {foreign}")

        row = conn.execute(
            """INSERT INTO tasks (project_id, key, title, specification, acceptance_criteria,
                                  assigned_role, priority, state, max_retries, budget_usd,
                                  allowed_paths)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                project["id"],
                key,
                title,
                specification,
                Jsonb(criteria),
                role,
                priority,
                INITIAL_STATE,
                max_retries,
                budget_usd,
                Jsonb(paths),
            ),
        ).fetchone()
        assert row is not None
        for d in deps:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (%s, %s)",
                (row["id"], d["id"]),
            )
        append_event(
            conn,
            actor=actor,
            type="TASK_CREATED",
            project_id=project["id"],
            task_id=row["id"],
            to_state=INITIAL_STATE,
            payload={"title": title, "dependencies": dep_keys},
        )
        promote_if_ready(conn, row["id"])
        return get_task(conn, key)


def set_task_budget(conn: Conn, actor: Actor, key: str, budget_usd: Decimal) -> Task:
    """Change a task's budget (humans only). Not a state transition, but always an event."""
    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans change budgets")
    if budget_usd < 0:
        raise InvalidInput("budget_usd must be >= 0")
    with conn.transaction():
        row = conn.execute("SELECT * FROM tasks WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"task {key} not found")
        conn.execute(
            "UPDATE tasks SET budget_usd = %s, updated_at = now() WHERE id = %s",
            (budget_usd, row["id"]),
        )
        append_event(
            conn,
            actor=actor,
            type="TASK_BUDGET_CHANGED",
            project_id=row["project_id"],
            task_id=row["id"],
            payload={"from": str(row["budget_usd"]), "to": str(budget_usd)},
        )
        return get_task(conn, key)
