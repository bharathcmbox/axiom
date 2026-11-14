"""Plans: a human's spec, a planner's proposal, a human's decision (ADR-011).

Legality is in `domain/plan_machine.py`; what makes a plan acceptable is in `plan_rules.py`. Each
function here is one transaction: lock the plan, check the table, update, append the event. Plan
events never move a task. Approval is the only way plan text becomes work.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from axiom.config import get_settings
from axiom.contracts import InvalidResult, PlanTask, parse_plan
from axiom.domain.plan_machine import MAX_PLAN_ATTEMPTS, PlanCommand, PlanState, next_plan_state
from axiom.domain.roles import Actor, ActorKind, Role
from axiom.errors import (
    AssignmentRejected,
    Forbidden,
    InvalidInput,
    LeaseExpired,
    NotExecutionOwner,
    NotFound,
)
from axiom.events.log import append_event
from axiom.orchestrator import budgets
from axiom.orchestrator.plan_rules import validate_plan
from axiom.orchestrator.tasks import create_task
from axiom.registry.agents import get_agent
from axiom.state.db import Conn

REAPER = Actor(ActorKind.SYSTEM, "reaper")
FEEDBACK_LIMIT = 4000


@dataclass(frozen=True)
class PlanAssignment:
    """Everything a planner needs, returned by a claim."""

    plan_id: int
    project_key: str
    repo_url: str | None
    default_branch: str
    specification: str
    budget_usd: Decimal
    test_paths: list[str]
    feedback: list[str]
    previous_plan: dict[str, Any] | None
    lease_expires_at: datetime
    run_budget_usd: Decimal
    run_number: int  # gives each run its own workspace, so a killed run's is still there


def _plan_event(
    conn: Conn,
    actor: Actor,
    plan: dict[str, Any],
    command: PlanCommand,
    event: str,
    target: PlanState,
    payload: dict[str, Any] | None = None,
) -> None:
    event_id = append_event(
        conn,
        actor=actor,
        type=event,
        project_id=plan["project_id"],
        plan_id=plan["id"],
        agent_id=plan["planner_agent_id"],
        payload={"from": plan["status"], "to": target, "command": command} | (payload or {}),
    )
    from axiom.orchestrator.notifications import notify
    from axiom.orchestrator.timers import on_plan_transition

    on_plan_transition(conn, plan, PlanState(plan["status"]), target)
    if target == PlanState.PROPOSED:
        notify(conn, "plan_ready", project_id=plan["project_id"], plan_id=plan["id"],
               dedup_key=f"plan_ready:event:{event_id}")  # fmt: skip


def _lock(conn: Conn, plan_id: int) -> dict[str, Any]:
    plan = conn.execute(
        """SELECT p.*, a.key AS planner_key, (p.lease_expires_at <= now()) AS lease_expired
           FROM plans p LEFT JOIN agents a ON a.id = p.planner_agent_id
           WHERE p.id = %s FOR UPDATE OF p""",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise NotFound(f"plan {plan_id} not found")
    return plan


def _require_owner(actor: Actor, plan: dict[str, Any]) -> None:
    if actor.kind != ActorKind.AGENT or actor.name != plan["planner_key"]:
        raise NotExecutionOwner(f"plan {plan['id']} is held by {plan['planner_key']}")
    if plan["lease_expired"]:
        raise LeaseExpired(f"lease on plan {plan['id']} has expired")


# ---- what a human does ----------------------------------------------------------------------


def request_plan(
    conn: Conn, actor: Actor, *, project_key: str, specification: str, budget_usd: Decimal
) -> dict[str, Any]:
    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans request plans")
    if not specification.strip():
        raise InvalidInput("a plan needs a specification")
    if budget_usd <= 0:
        raise InvalidInput("a plan's budget must be above zero")
    with conn.transaction():
        project = conn.execute("SELECT id FROM projects WHERE key = %s", (project_key,)).fetchone()
        if project is None:
            raise NotFound(f"project {project_key} not found")
        return insert_plan(conn, actor, project["id"], specification, budget_usd)


def insert_plan(
    conn: Conn,
    actor: Actor,
    project_id: int,
    specification: str,
    budget_usd: Decimal,
    *,
    timer_id: int | None = None,
) -> dict[str, Any]:
    """A new REQUESTED plan, inside the caller's transaction: a human's request, or a schedule
    the human set up firing (ADR-018). Either way, only a human can approve what it proposes."""
    plan = conn.execute(
        """INSERT INTO plans (project_id, specification, budget_usd, status, created_by, timer_id)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (project_id, specification, budget_usd, PlanState.REQUESTED, str(actor), timer_id),
    ).fetchone()
    assert plan is not None
    append_event(
        conn,
        actor=actor,
        type="PLAN_REQUESTED",
        project_id=project_id,
        plan_id=plan["id"],
        timer_id=timer_id,
        payload={"budget_usd": str(budget_usd)},
    )
    return plan


def reject_plan(conn: Conn, actor: Actor, plan_id: int, feedback: str) -> dict[str, Any]:
    """Back to the planner with your words. Not a failure, so the attempts start over."""
    if not feedback.strip():
        raise InvalidInput("say what should change: the next attempt reads it")
    with conn.transaction():
        plan = _lock(conn, plan_id)
        target = next_plan_state(PlanCommand.REJECT, PlanState(plan["status"]), actor.kind)
        text = f"{actor} rejected the last plan: {feedback[:FEEDBACK_LIMIT]}"
        updated = conn.execute(
            """UPDATE plans SET status = %s, attempt_count = 0, error = NULL,
                      feedback = feedback || %s WHERE id = %s RETURNING *""",
            (target, Jsonb([text]), plan_id),
        ).fetchone()
        assert updated is not None
        _plan_event(
            conn, actor, plan, PlanCommand.REJECT, "PLAN_REJECTED", target, {"feedback": feedback}
        )
        return updated


def cancel_plan(
    conn: Conn, actor: Actor, plan_id: int, reason: str | None = None
) -> dict[str, Any]:
    with conn.transaction():
        plan = _lock(conn, plan_id)
        target = next_plan_state(PlanCommand.CANCEL, PlanState(plan["status"]), actor.kind)
        updated = conn.execute(
            """UPDATE plans SET status = %s, decided_by = %s, decided_at = now()
               WHERE id = %s RETURNING *""",
            (target, str(actor), plan_id),
        ).fetchone()
        assert updated is not None
        _plan_event(
            conn, actor, plan, PlanCommand.CANCEL, "PLAN_CANCELLED", target, {"reason": reason}
        )
        return updated


def _in_dependency_order(tasks: list[PlanTask]) -> list[PlanTask]:
    ordered: list[PlanTask] = []
    done: set[str] = set()
    waiting = list(tasks)
    while waiting:
        ready = [t for t in waiting if set(t.depends_on) <= done]
        assert ready, "validated plans have no cycles"
        ordered += ready
        done |= {t.ref for t in ready}
        waiting = [t for t in waiting if t.ref not in done]
    return ordered


def approve_plan(conn: Conn, actor: Actor, plan_id: int) -> dict[str, Any]:
    """Create every task, or none: one transaction, with the approving human as their creator.
    The rules run again, because the plan's limits or the project's test paths may have changed."""
    with conn.transaction():
        plan = _lock(conn, plan_id)
        target = next_plan_state(PlanCommand.APPROVE, PlanState(plan["status"]), actor.kind)
        project = conn.execute(
            "SELECT key, test_paths FROM projects WHERE id = %s", (plan["project_id"],)
        ).fetchone()
        assert project is not None
        contract = parse_plan(plan["contract"])
        problems = validate_plan(
            contract, budget_usd=plan["budget_usd"], test_paths=project["test_paths"]
        )
        if problems:
            raise InvalidInput(f"this plan no longer passes its checks: {'; '.join(problems)}")
        keys: dict[str, str] = {}
        for item in _in_dependency_order(contract.tasks):
            task = create_task(
                conn,
                actor,
                project_key=project["key"],
                title=item.title,
                specification=item.specification,
                acceptance_criteria=item.acceptance_criteria,
                assigned_role=item.role,
                priority=item.priority,
                budget_usd=Decimal(str(item.budget_usd)),
                dependencies=[keys[d] for d in item.depends_on],
                allowed_paths=item.allowed_paths,
            )
            conn.execute(
                "UPDATE tasks SET plan_id = %s, plan_ref = %s WHERE id = %s",
                (plan_id, item.ref, task.id),
            )
            keys[item.ref] = task.key
        updated = conn.execute(
            """UPDATE plans SET status = %s, decided_by = %s, decided_at = now()
               WHERE id = %s RETURNING *""",
            (target, str(actor), plan_id),
        ).fetchone()
        assert updated is not None
        _plan_event(
            conn, actor, plan, PlanCommand.APPROVE, "PLAN_APPROVED", target, {"tasks": keys}
        )
        return updated


# ---- what a planner does --------------------------------------------------------------------


def claim_plan(
    conn: Conn, actor: Actor, *, lease_seconds: int | None = None
) -> PlanAssignment | None:
    """A planner agent claims the oldest requested plan, or gets None."""
    if actor.kind != ActorKind.AGENT:
        raise Forbidden("only agents claim plans")
    agent = get_agent(conn, actor.name)
    if agent.role != Role.ENGINEERING_MANAGER:
        raise Forbidden(f"agent {agent.key} is a {agent.role}, not a planner")
    settings = get_settings()
    lease = lease_seconds or settings.plan_lease_seconds
    with conn.transaction():
        row = conn.execute(
            """SELECT id FROM plans WHERE status = 'REQUESTED'
               ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"""
        ).fetchone()
        if row is None:
            return None
        if not agent.enabled:
            raise AssignmentRejected(f"agent {agent.key} is disabled")
        conn.execute("SELECT 1 FROM agents WHERE id = %s FOR UPDATE", (agent.id,))
        held = conn.execute(
            "SELECT count(*) AS n FROM plans WHERE planner_agent_id = %s AND status = 'CLAIMED'",
            (agent.id,),
        ).fetchone()
        if held is not None and held["n"] >= agent.max_concurrent:
            raise AssignmentRejected(f"agent {agent.key} is at capacity")
        plan = _lock(conn, row["id"])
        run_budget = budgets.reserve_from_the_day(
            conn, settings.plan_run_budget_usd, minimum=settings.plan_min_run_usd
        )
        target = next_plan_state(PlanCommand.CLAIM, PlanState(plan["status"]), actor.kind)
        updated = conn.execute(
            """UPDATE plans SET status = %s, planner_agent_id = %s, run_budget_usd = %s,
                      lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s RETURNING lease_expires_at""",
            (target, agent.id, run_budget, lease, plan["id"]),
        ).fetchone()
        assert updated is not None
        append_event(
            conn,
            actor=actor,
            type="PLAN_CLAIMED",
            project_id=plan["project_id"],
            plan_id=plan["id"],
            agent_id=agent.id,
            payload={"run_budget_usd": str(run_budget)},
        )
        project = conn.execute(
            "SELECT * FROM projects WHERE id = %s", (plan["project_id"],)
        ).fetchone()
        assert project is not None
        claims = conn.execute(
            "SELECT count(*) AS n FROM events WHERE plan_id = %s AND type = 'PLAN_CLAIMED'",
            (plan["id"],),
        ).fetchone()
        assert claims is not None
        return PlanAssignment(
            run_number=int(claims["n"]),
            plan_id=plan["id"],
            project_key=project["key"],
            repo_url=project["repo_url"],
            default_branch=project["default_branch"],
            specification=plan["specification"],
            budget_usd=plan["budget_usd"],
            test_paths=project["test_paths"],
            feedback=plan["feedback"],
            previous_plan=plan["contract"],
            lease_expires_at=updated["lease_expires_at"],
            run_budget_usd=run_budget,
        )


def plan_heartbeat(
    conn: Conn, actor: Actor, plan_id: int, *, lease_seconds: int | None = None
) -> datetime:
    lease = lease_seconds or get_settings().plan_lease_seconds
    with conn.transaction():
        plan = _lock(conn, plan_id)
        if plan["status"] != PlanState.CLAIMED:
            raise InvalidInput(f"plan {plan_id} is {plan['status']}, not CLAIMED")
        _require_owner(actor, plan)
        row = conn.execute(
            """UPDATE plans SET lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s RETURNING lease_expires_at""",
            (lease, plan_id),
        ).fetchone()
        assert row is not None
        expires: datetime = row["lease_expires_at"]
        return expires


def _add_cost_sql() -> str:
    return """cost_usd = CASE WHEN %(cost)s::numeric IS NULL THEN cost_usd
                              ELSE coalesce(cost_usd, 0) + %(cost)s::numeric END,
              cost_estimated = cost_estimated OR %(estimated)s"""


def submit_plan(
    conn: Conn,
    actor: Actor,
    plan_id: int,
    contract: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    """Returns the plan's new status. A plan that fails the rules counts as a failed attempt, and
    its problems go to the next one. A human never sees it."""
    payload = payload or {}
    cost_usd, _, _ = budgets.parse_cost(payload)
    with conn.transaction():
        plan = _lock(conn, plan_id)
        next_plan_state(PlanCommand.SUBMIT, PlanState(plan["status"]), actor.kind)
        _require_owner(actor, plan)
        project = conn.execute(
            "SELECT test_paths FROM projects WHERE id = %s", (plan["project_id"],)
        ).fetchone()
        assert project is not None
        try:
            parsed = parse_plan(contract)
            problems = validate_plan(
                parsed, budget_usd=plan["budget_usd"], test_paths=project["test_paths"]
            )
        except InvalidResult as e:
            parsed, problems = None, [f"invalid plan: {e}"]
        if problems or parsed is None:
            return _retry_or_error(
                conn,
                actor,
                plan,
                PlanCommand.FAIL_RUN,
                "PLAN_RUN_FAILED",
                "; ".join(problems),
                payload,
                feed_back=True,
            )
        target = next_plan_state(PlanCommand.SUBMIT, PlanState(plan["status"]), actor.kind)
        conn.execute(
            f"""UPDATE plans SET status = %(status)s, contract = %(contract)s, error = NULL,
                       lease_expires_at = NULL, {_add_cost_sql()}
                WHERE id = %(id)s""",
            {
                "status": target,
                "contract": Jsonb(parsed.model_dump(mode="json")),
                "cost": cost_usd,
                "estimated": budgets.is_estimated(payload) and cost_usd is not None,
                "id": plan_id,
            },
        )
        _plan_event(
            conn,
            actor,
            plan,
            PlanCommand.SUBMIT,
            "PLAN_PROPOSED",
            target,
            {
                "tasks": len(parsed.tasks),
                "cost_usd": str(cost_usd) if cost_usd is not None else None,
            },
        )
        return target


def fail_plan_run(
    conn: Conn,
    actor: Actor,
    plan_id: int,
    error: str,
    payload: dict[str, Any] | None = None,
    *,
    final: bool = False,
) -> str:
    """`final`: it ran out of budget, so a retry would fail the same way."""
    with conn.transaction():
        plan = _lock(conn, plan_id)
        next_plan_state(
            PlanCommand.FAIL_RUN,
            PlanState(plan["status"]),
            actor.kind,
            attempts_after=plan["attempt_count"] + 1,
        )
        _require_owner(actor, plan)
        return _retry_or_error(
            conn,
            actor,
            plan,
            PlanCommand.FAIL_RUN,
            "PLAN_RUN_FAILED",
            error,
            payload or {},
            final=final,
        )


def _retry_or_error(
    conn: Conn,
    actor: Actor,
    plan: dict[str, Any],
    command: PlanCommand,
    event: str,
    error: str,
    payload: dict[str, Any],
    *,
    feed_back: bool = False,
    final: bool = False,
) -> str:
    attempts = plan["attempt_count"] + 1
    target = next_plan_state(
        command,
        PlanState(plan["status"]),
        actor.kind,
        attempts_after=MAX_PLAN_ATTEMPTS if final else attempts,
    )
    cost_usd, _, _ = budgets.parse_cost(payload)
    feedback = [f"Your last plan was refused: {error[:FEEDBACK_LIMIT]}"] if feed_back else []
    conn.execute(
        f"""UPDATE plans SET status = %(status)s, attempt_count = %(attempts)s,
                   lease_expires_at = NULL, error = %(error)s, feedback = feedback || %(feedback)s,
                   {_add_cost_sql()}
            WHERE id = %(id)s""",
        {
            "status": target,
            "attempts": attempts,
            "error": error[:2000],
            "feedback": Jsonb(feedback),
            "cost": cost_usd,
            "estimated": budgets.is_estimated(payload) and cost_usd is not None,
            "id": plan["id"],
        },
    )
    _plan_event(
        conn, actor, plan, command, event, target, {"error": error[:500], "attempts": attempts}
    )
    return target


def record_plan_cost(conn: Conn, actor: Actor, plan_id: int, payload: dict[str, Any]) -> None:
    """A planning run that ended without reporting (lease lost, worker killed). The money was spent
    whatever the plan's state, and a plan has several attempts, so this adds to its cost."""
    is_planner = (
        actor.kind == ActorKind.AGENT
        and get_agent(conn, actor.name).role == Role.ENGINEERING_MANAGER
    )
    if not is_planner:
        raise Forbidden("only planners report planning costs")
    cost_usd, _, _ = budgets.parse_cost(payload)
    if cost_usd is None:
        raise InvalidInput("cost.usd is required")
    with conn.transaction():
        plan = _lock(conn, plan_id)
        conn.execute(
            f"UPDATE plans SET {_add_cost_sql()} WHERE id = %(id)s",
            {"cost": cost_usd, "estimated": budgets.is_estimated(payload), "id": plan_id},
        )
        append_event(
            conn,
            actor=actor,
            type="PLAN_COST_RECORDED",
            project_id=plan["project_id"],
            plan_id=plan_id,
            payload={"cost_usd": str(cost_usd), "estimated": budgets.is_estimated(payload)},
        )


def expire_plan_leases(conn: Conn) -> list[int]:
    """Reaper: time out CLAIMED plans whose lease expired."""
    ids = [
        r["id"]
        for r in conn.execute(
            """SELECT id FROM plans WHERE status = 'CLAIMED' AND lease_expires_at <= now()
               ORDER BY id"""
        ).fetchall()
    ]
    done = []
    for plan_id in ids:
        with conn.transaction():
            plan = _lock(conn, plan_id)
            if plan["status"] != PlanState.CLAIMED or not plan["lease_expired"]:
                continue  # renewed or finished meanwhile
            _retry_or_error(
                conn, REAPER, plan, PlanCommand.TIME_OUT, "PLAN_TIMED_OUT", "lease expired", {}
            )
            done.append(plan_id)
    return done


# ---- reading --------------------------------------------------------------------------------


def get_plan(conn: Conn, plan_id: int) -> dict[str, Any]:
    plan = conn.execute(
        """SELECT p.*, pr.key AS project_key, a.key AS planner_key FROM plans p
           JOIN projects pr ON pr.id = p.project_id
           LEFT JOIN agents a ON a.id = p.planner_agent_id WHERE p.id = %s""",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise NotFound(f"plan {plan_id} not found")
    return plan | {"task_keys": _task_keys(conn, plan_id)}


def _task_keys(conn: Conn, plan_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT plan_ref, key FROM tasks WHERE plan_id = %s ORDER BY id", (plan_id,)
    ).fetchall()
    return {r["plan_ref"]: r["key"] for r in rows}


def list_plans(
    conn: Conn, *, project_key: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.*, pr.key AS project_key, a.key AS planner_key FROM plans p
           JOIN projects pr ON pr.id = p.project_id
           LEFT JOIN agents a ON a.id = p.planner_agent_id
           WHERE (%(project)s::text IS NULL OR pr.key = %(project)s)
             AND (%(status)s::text IS NULL OR p.status = %(status)s)
           ORDER BY p.id""",
        {"project": project_key, "status": status},
    ).fetchall()
    return [row | {"task_keys": _task_keys(conn, row["id"])} for row in rows]


def plan_events(conn: Conn, plan_id: int) -> list[dict[str, Any]]:
    get_plan(conn, plan_id)
    return conn.execute(
        "SELECT * FROM events WHERE plan_id = %s ORDER BY id", (plan_id,)
    ).fetchall()
