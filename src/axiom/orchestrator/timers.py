"""Timers (ADR-018): schedules a human sets up, and follow-ups Axiom sets for itself.

A schedule requests a plan each time it fires; the plan waits for a human's approval like any other.
A follow-up fires once if what it watches is still waiting for a human (a question, an escalation,
a PR to merge, a plan to approve), and is cancelled as soon as that moves on. Follow-ups are created
and cancelled in the same transaction as the transition that causes them.

The reaper fires due timers (`fire_due_timers`) on the DB clock. Each timer is fired in its own
transaction, with its row locked and its state checked, so it can't fire twice. A timer that came
due while Axiom was down fires once; missed occurrences aren't replayed.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from axiom.config import get_settings
from axiom.domain.plan_machine import PlanState
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskState
from axiom.domain.timer_machine import TimerCommand, TimerState, check_timer, next_timer_state
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.orchestrator import budgets
from axiom.state.db import Conn

SCHEDULER = Actor(ActorKind.SYSTEM, "scheduler")
FOLLOW_UPS = Actor(ActorKind.SYSTEM, "follow-ups")
MIN_EVERY_HOURS = 1
MAX_EVERY_HOURS = 24 * 90


def task_follow_ups() -> dict[TaskState, tuple[str, int]]:
    s = get_settings()
    return {
        TaskState.WAITING_FOR_HUMAN: ("waiting_for_human", s.follow_up_waiting_seconds),
        TaskState.AWAITING_MERGE: ("awaiting_merge", s.follow_up_merge_seconds),
    }


def plan_follow_ups() -> dict[PlanState, tuple[str, int]]:
    return {PlanState.PROPOSED: ("plan_proposed", get_settings().follow_up_plan_seconds)}


def _event(
    conn: Conn,
    actor: Actor,
    timer: dict[str, Any],
    event: str,
    target: TimerState,
    payload: dict[str, Any] | None = None,
) -> None:
    """Timer events carry `timer_id`, and their subject only in the payload: a task's or a plan's
    own events stay its state transitions, which the invariants replay."""
    subject = {k: timer[k] for k in ("task_id", "plan_id") if timer[k] is not None}
    append_event(
        conn,
        actor=actor,
        type=event,
        project_id=timer["project_id"],
        timer_id=timer["id"],
        payload={"kind": timer["kind"], "to": target}
        | ({} if event == "TIMER_CREATED" else {"from": timer["status"]})
        | subject
        | (payload or {}),
    )


# ---- what a human does ----------------------------------------------------------------------


def create_schedule(
    conn: Conn,
    actor: Actor,
    *,
    project_key: str,
    specification: str,
    budget_usd: Decimal,
    every_hours: int,
    first_at: datetime | None = None,
) -> dict[str, Any]:
    """A recurring request for a plan. Only a human may set one up (ADR-018)."""
    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans set up schedules")
    if not specification.strip():
        raise InvalidInput("a schedule needs a specification")
    if budget_usd <= 0:
        raise InvalidInput("a schedule's plan budget must be above zero")
    if not MIN_EVERY_HOURS <= every_hours <= MAX_EVERY_HOURS:
        raise InvalidInput(f"every_hours must be {MIN_EVERY_HOURS} to {MAX_EVERY_HOURS}")
    every = every_hours * 3600
    with conn.transaction():
        project = conn.execute("SELECT id FROM projects WHERE key = %s", (project_key,)).fetchone()
        if project is None:
            raise NotFound(f"project {project_key} not found")
        timer = conn.execute(
            """INSERT INTO timers (kind, status, project_id, due_at, every_seconds, specification,
                                   budget_usd, created_by)
               VALUES ('schedule', 'PENDING', %s,
                       coalesce(%s::timestamptz, now() + make_interval(secs => %s)),
                       %s, %s, %s, %s)
               RETURNING *""",
            (project["id"], first_at, every, every, specification, budget_usd, str(actor)),
        ).fetchone()
        assert timer is not None
        _event(conn, actor, timer, "TIMER_CREATED", TimerState.PENDING,
               {"every_hours": every_hours, "due_at": timer["due_at"].isoformat()})  # fmt: skip
        return timer


def cancel_timer(conn: Conn, actor: Actor, timer_id: int) -> dict[str, Any]:
    with conn.transaction():
        timer = _lock(conn, timer_id)
        return _cancel(conn, actor, timer)


def _lock(conn: Conn, timer_id: int) -> dict[str, Any]:
    timer = conn.execute("SELECT * FROM timers WHERE id = %s FOR UPDATE", (timer_id,)).fetchone()
    if timer is None:
        raise NotFound(f"timer {timer_id} not found")
    return timer


def _cancel(
    conn: Conn, actor: Actor, timer: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    rule = check_timer(TimerCommand.CANCEL, TimerState(timer["status"]), actor.kind)
    target = next_timer_state(TimerCommand.CANCEL, TimerState(timer["status"]), actor.kind)
    row = conn.execute(
        """UPDATE timers SET status = %s, ended_by = %s, ended_at = now()
           WHERE id = %s RETURNING *""",
        (target, str(actor), timer["id"]),
    ).fetchone()
    assert row is not None
    _event(conn, actor, timer, rule.event, target, payload)
    return row


def list_timers(conn: Conn, status: str | None = None) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT tm.*, p.key AS project_key, t.key AS task_key FROM timers tm
           JOIN projects p ON p.id = tm.project_id LEFT JOIN tasks t ON t.id = tm.task_id
           WHERE %(s)s::text IS NULL OR tm.status = %(s)s ORDER BY tm.id""",
        {"s": status},
    ).fetchall()


# ---- follow-ups, set and cleared by transitions ----------------------------------------------


def on_task_transition(
    conn: Conn, task: dict[str, Any], from_state: TaskState, to_state: TaskState
) -> None:
    """Called by apply_command in the transition's transaction."""
    if from_state == to_state:
        return
    _clear(conn, "task_id", task["id"], from_state)
    follow_up = task_follow_ups().get(to_state)
    if follow_up:
        _set(conn, task["project_id"], "task_id", task["id"], to_state, *follow_up)


def on_plan_transition(
    conn: Conn, plan: dict[str, Any], from_state: PlanState, to_state: PlanState
) -> None:
    """Called by the plan service in the transition's transaction."""
    if from_state == to_state:
        return
    _clear(conn, "plan_id", plan["id"], from_state)
    follow_up = plan_follow_ups().get(to_state)
    if follow_up:
        _set(conn, plan["project_id"], "plan_id", plan["id"], to_state, *follow_up)


_SUBJECTS = {"task_id": "task_id", "plan_id": "plan_id"}  # column names, never user input


def _clear(conn: Conn, column: str, subject_id: int, left: str) -> None:
    col = _SUBJECTS[column]
    rows = conn.execute(
        f"""SELECT * FROM timers WHERE {col} = %s AND subject_state = %s AND status = 'PENDING'
            FOR UPDATE""",
        (subject_id, str(left)),
    ).fetchall()
    for timer in rows:
        _cancel(conn, FOLLOW_UPS, timer, {"reason": timer["reason"], "moved_on_from": str(left)})


def _set(
    conn: Conn, project_id: int, column: str, subject_id: int, state: str, reason: str, after: int
) -> None:
    col = _SUBJECTS[column]
    timer = conn.execute(
        f"""INSERT INTO timers (kind, status, project_id, due_at, {col}, reason, subject_state,
                                created_by)
            VALUES ('follow_up', 'PENDING', %s, now() + make_interval(secs => %s), %s, %s, %s, %s)
            RETURNING *""",
        (project_id, after, subject_id, reason, str(state), str(FOLLOW_UPS)),
    ).fetchone()
    assert timer is not None
    _event(conn, FOLLOW_UPS, timer, "TIMER_CREATED", TimerState.PENDING,
           {"reason": reason, "due_at": timer["due_at"].isoformat()})  # fmt: skip


# ---- firing ---------------------------------------------------------------------------------


def fire_due_timers(conn: Conn) -> list[int]:
    """Fire every timer that is due, each in its own transaction. Returns their ids."""
    fired: list[int] = []
    for _ in range(get_settings().max_timers_per_sweep):
        with conn.transaction():
            timer = conn.execute(
                """SELECT * FROM timers WHERE status = 'PENDING' AND due_at <= now()
                   ORDER BY due_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"""
            ).fetchone()
            if timer is None:
                break
            _fire(conn, timer)
            fired.append(timer["id"])
    return fired


def _fire(conn: Conn, timer: dict[str, Any]) -> None:
    if timer["kind"] == "follow_up" and not _still_waiting(conn, timer):
        # Defensive: transitions clear their follow-ups, so this means one was missed.
        _cancel(conn, FOLLOW_UPS, timer, {"reason": timer["reason"], "moved_on": True})
        return
    recurring = timer["every_seconds"] is not None
    target = next_timer_state(TimerCommand.FIRE, TimerState.PENDING, ActorKind.SYSTEM,
                              recurring=recurring)  # fmt: skip
    payload: dict[str, Any] = {}
    if timer["kind"] == "schedule":
        payload = _request_plan(conn, timer)
    else:
        payload = {"reason": timer["reason"], "waiting_in": timer["subject_state"]}
    row = conn.execute(
        """UPDATE timers SET
             status = %(target)s,
             fired_count = fired_count + 1,
             last_fired_at = now(),
             -- next occurrence strictly after now; occurrences missed while down are skipped
             due_at = CASE WHEN every_seconds IS NULL THEN due_at ELSE due_at
                 + make_interval(secs => every_seconds
                     * (floor(extract(epoch FROM now() - due_at) / every_seconds) + 1)) END,
             ended_by = CASE WHEN %(target)s = 'PENDING' THEN NULL ELSE %(actor)s END,
             ended_at = CASE WHEN %(target)s = 'PENDING' THEN NULL ELSE now() END
           WHERE id = %(id)s
           RETURNING due_at, floor(extract(epoch FROM now() - %(due)s::timestamptz)
                                   / nullif(every_seconds, 0))::int AS missed""",
        {"target": target, "actor": str(SCHEDULER), "id": timer["id"], "due": timer["due_at"]},
    ).fetchone()
    assert row is not None
    if recurring:
        payload |= {"next_due_at": row["due_at"].isoformat(), "missed": row["missed"] or 0}
    _event(conn, SCHEDULER, timer, "TIMER_FIRED", target, payload)


def _still_waiting(conn: Conn, timer: dict[str, Any]) -> bool:
    if timer["task_id"] is not None:
        row = conn.execute("SELECT state FROM tasks WHERE id = %s", (timer["task_id"],)).fetchone()
        return row is not None and row["state"] == timer["subject_state"]
    row = conn.execute("SELECT status FROM plans WHERE id = %s", (timer["plan_id"],)).fetchone()
    return row is not None and row["status"] == timer["subject_state"]


def _request_plan(conn: Conn, timer: dict[str, Any]) -> dict[str, Any]:
    """Ask for a plan, unless the day's budget couldn't pay for planning it (ADR-018: refused and
    recorded, never skipped silently)."""
    from axiom.orchestrator.plans import insert_plan

    settings = get_settings()
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (budgets.BUDGET_LOCK_ID,))
    left = settings.daily_budget_usd - budgets.daily_spent(conn) - budgets.reserved(conn)
    if left < settings.plan_min_run_usd:
        have = max(left, Decimal(0))
        need = settings.plan_min_run_usd
        return {"refused": f"the day's budget has ${have:.2f} left, under the ${need} a plan needs"}
    plan = insert_plan(conn, SCHEDULER, timer["project_id"], timer["specification"],
                       timer["budget_usd"], timer_id=timer["id"])  # fmt: skip
    return {"plan_id": plan["id"], "on_behalf_of": timer["created_by"]}
