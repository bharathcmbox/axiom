"""Load what metrics need from the database, and record the facts only a human knows."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from axiom import metrics as m
from axiom.domain.roles import Actor, ActorKind
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.state.db import Conn

DEMO_TAG = "demo"


def _require_human(actor: Actor) -> None:
    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans record this")


def _load(
    conn: Conn, project_key: str | None, include_demo: bool
) -> tuple[list[m.TaskMetrics], dict[str, list[dict[str, Any]]], dict[str, int], datetime]:
    """Per-task metrics, each task's events by key, review verdict counts, and the DB clock."""
    tasks = conn.execute(
        """SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id
           WHERE (%(p)s::text IS NULL OR p.key = %(p)s)
             AND (%(demo)s OR NOT t.tags ? 'demo')
           ORDER BY t.id""",
        {"p": project_key, "demo": include_demo},
    ).fetchall()
    ids = [t["id"] for t in tasks]
    events: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for e in conn.execute("SELECT * FROM events WHERE task_id = ANY(%s) ORDER BY id", (ids,)):
        events[e["task_id"]].append(e)
    dev = _costs(conn, "executions", ids)
    rev = _costs(conn, "reviews", ids)
    verdicts: dict[str, int] = {}
    for r in conn.execute(
        """SELECT verdict, count(*) AS n FROM reviews
           WHERE task_id = ANY(%s) AND verdict IS NOT NULL GROUP BY verdict""",
        (ids,),
    ):
        verdicts[r["verdict"]] = r["n"]
    # Security findings from reviews that ran: a carried-forward PASS is a copy, not a new look.
    security: dict[int, tuple[int, int]] = {}
    for r in conn.execute(
        """SELECT r.task_id,
                  sum(jsonb_array_length(coalesce(r.contract->'findings', '[]'))) AS n,
                  sum((SELECT count(*) FROM jsonb_array_elements(
                         coalesce(r.contract->'findings', '[]')) f
                       WHERE f->>'severity' IN ('high', 'critical'))) AS blocking
           FROM reviews r
           WHERE r.task_id = ANY(%s) AND r.role = 'security_reviewer' AND r.verdict IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM events e WHERE e.review_id = r.id
                             AND e.type = 'REVIEW_CARRIED_FORWARD')
           GROUP BY r.task_id""",
        (ids,),
    ):
        security[r["task_id"]] = (int(r["n"]), int(r["blocking"]))
    now_row = conn.execute("SELECT now() AS now").fetchone()
    assert now_row is not None
    now: datetime = now_row["now"]
    zero = Decimal(0)
    per_task = [
        m.task_metrics(
            t,
            events[t["id"]],
            dev.get(t["id"], zero),
            rev.get(t["id"], zero),
            now,
            security.get(t["id"], (0, 0)),
        )
        for t in tasks
    ]
    by_key = {t["key"]: events[t["id"]] for t in tasks}
    return per_task, by_key, verdicts, now


def collect(
    conn: Conn, *, project_key: str | None = None, include_demo: bool = False
) -> tuple[list[m.TaskMetrics], m.Summary, list[dict[str, Any]]]:
    """Per-task metrics, their summary, and the recorded baselines."""
    per_task, _, verdicts, _ = _load(conn, project_key, include_demo)
    baselines = conn.execute(
        """SELECT e.occurred_at, e.actor, e.payload FROM events e
           LEFT JOIN projects p ON p.id = e.project_id
           WHERE e.type = 'BASELINE_RECORDED' AND (%(p)s::text IS NULL OR p.key = %(p)s)
           ORDER BY e.id""",
        {"p": project_key},
    ).fetchall()
    return per_task, m.summarize(per_task, verdicts), baselines


def collect_plans(
    conn: Conn, *, project_key: str | None = None, include_demo: bool = False
) -> list[m.PlanDelivery]:
    """Delivery per approved plan: from the specification to merged work."""
    per_task, events, _, now = _load(conn, project_key, include_demo)
    plans = conn.execute(
        """SELECT pl.id, pl.created_at, array_agg(t.key ORDER BY t.id) AS keys
           FROM plans pl JOIN projects p ON p.id = pl.project_id
           JOIN tasks t ON t.plan_id = pl.id
           WHERE pl.status = 'APPROVED' AND (%(p)s::text IS NULL OR p.key = %(p)s)
           GROUP BY pl.id ORDER BY pl.id""",
        {"p": project_key},
    ).fetchall()
    by_key = {t.key: t for t in per_task}
    out = []
    for pl in plans:
        tasks = [by_key[k] for k in pl["keys"] if k in by_key]
        if tasks:
            out.append(m.plan_delivery(pl["id"], pl["created_at"], tasks, events, now))
    return out


def _costs(conn: Conn, table: str, ids: list[int]) -> dict[int, Decimal]:
    query = "SELECT task_id, sum(cost_usd) AS c FROM {} WHERE task_id = ANY(%s) GROUP BY task_id"
    assert table in ("executions", "reviews")
    rows = conn.execute(query.format(table), (ids,))
    return {r["task_id"]: r["c"] or Decimal(0) for r in rows}


def set_tags(conn: Conn, actor: Actor, key: str, tags: list[str]) -> list[str]:
    _require_human(actor)
    if not all(isinstance(t, str) and t.strip() for t in tags):
        raise InvalidInput("tags must be non-empty strings")
    clean = sorted(set(tags))
    with conn.transaction():
        row = conn.execute("SELECT * FROM tasks WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"task {key} not found")
        conn.execute("UPDATE tasks SET tags = %s WHERE id = %s", (Jsonb(clean), row["id"]))
        append_event(
            conn,
            actor=actor,
            type="TASK_TAGGED",
            project_id=row["project_id"],
            task_id=row["id"],
            payload={"tags": clean},
        )
    return clean


def record_defect(conn: Conn, actor: Actor, key: str, description: str, severity: str) -> None:
    """A defect found after merge: only a human knows, and SPEC section 11 wants it counted."""
    _require_human(actor)
    if severity not in ("low", "medium", "high", "critical"):
        raise InvalidInput(f"unknown severity {severity!r}")
    if not description.strip():
        raise InvalidInput("description must not be empty")
    row = conn.execute("SELECT id, project_id FROM tasks WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise NotFound(f"task {key} not found")
    append_event(
        conn,
        actor=actor,
        type="DEFECT_RECORDED",
        project_id=row["project_id"],
        task_id=row["id"],
        payload={"description": description, "severity": severity},
    )


def record_baseline(
    conn: Conn,
    actor: Actor,
    *,
    project_key: str,
    title: str,
    minutes: float,
    cost_usd: Decimal,
    review_rounds: int = 0,
    notes: str = "",
) -> None:
    """A comparable task driven by hand in one session: SPEC's main question needs this."""
    _require_human(actor)
    if not title.strip() or minutes < 0 or cost_usd < 0 or review_rounds < 0:
        raise InvalidInput("baseline needs a title and non-negative numbers")
    row = conn.execute("SELECT id FROM projects WHERE key = %s", (project_key,)).fetchone()
    if row is None:
        raise NotFound(f"project {project_key} not found")
    append_event(
        conn,
        actor=actor,
        type="BASELINE_RECORDED",
        project_id=row["id"],
        payload={
            "title": title,
            "minutes": minutes,
            "cost_usd": str(cost_usd),
            "review_rounds": review_rounds,
            "notes": notes,
        },
    )
