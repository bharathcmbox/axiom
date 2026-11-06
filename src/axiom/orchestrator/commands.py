"""The command engine: the ONLY code that changes task or execution state.

Every command runs in one transaction:
  lock the task row -> check the transition table and guards -> update task (and execution)
  -> append the event.
An illegal command raises before anything is written.
"""

from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from axiom.config import get_settings
from axiom.domain import execution_machine as em
from axiom.domain.roles import Actor, ActorKind, Role
from axiom.domain.task_machine import (
    EXECUTING_STATES,
    TaskCommand,
    TaskState,
    check,
    next_state,
)
from axiom.errors import (
    AssignmentRejected,
    DependenciesNotMet,
    InvalidInput,
    LeaseExpired,
    LeaseNotExpired,
    NotExecutionOwner,
    NotFound,
)
from axiom.events.log import append_event
from axiom.orchestrator import budgets, review_store
from axiom.orchestrator.dependencies import dependencies_satisfied, pending_dependents
from axiom.orchestrator.tasks import Task, get_task
from axiom.registry.agents import Agent, get_agent
from axiom.state.db import Conn

SYSTEM_DEPENDENCIES = Actor(ActorKind.SYSTEM, "dependencies")
DEVELOPER_CAP_LOCK_ID = 7_351_002  # advisory lock guarding the global developer cap


def apply_command(
    conn: Conn,
    actor: Actor,
    task_key: str,
    command: TaskCommand,
    *,
    agent_key: str | None = None,
    payload: dict[str, Any] | None = None,
    lease_seconds: int | None = None,
) -> Task:
    """Apply `command` to one task. `claim` goes through assignment.claim_next instead."""
    if command == TaskCommand.CLAIM:
        raise InvalidInput("agents claim work with claim_next, not on a specific task")
    with conn.transaction():
        row = lock_task(conn, task_key)
        apply_locked(
            conn,
            row,
            command,
            actor,
            agent_key=agent_key,
            payload=payload or {},
            lease_seconds=lease_seconds,
        )
        return get_task(conn, task_key)


def lock_task(conn: Conn, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE key = %s FOR UPDATE", (key,)).fetchone()
    if row is None:
        raise NotFound(f"task {key} not found")
    return row


def promote_if_ready(conn: Conn, task_id: int) -> bool:
    """Promote a PENDING task whose dependencies are all DONE. Caller is in a transaction.

    Locks the task row before reading dependency states. Under READ COMMITTED each statement
    sees rows committed before it started, so when two dependencies are merged concurrently,
    whichever transaction locks the dependent second sees both as DONE and promotes it.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
    assert row is not None
    if row["state"] != TaskState.PENDING or not dependencies_satisfied(conn, task_id):
        return False
    apply_locked(conn, row, TaskCommand.PROMOTE, SYSTEM_DEPENDENCIES, payload={})
    return True


def apply_locked(
    conn: Conn,
    row: dict[str, Any],
    command: TaskCommand,
    actor: Actor,
    *,
    agent_key: str | None = None,
    payload: dict[str, Any],
    lease_seconds: int | None = None,
) -> None:
    """Apply a command to a task row the caller has already locked, inside its transaction."""
    state = TaskState(row["state"])
    rule = check(command, state, actor.kind)  # raises IllegalTransition / ActorNotPermitted
    lease = lease_seconds if lease_seconds is not None else get_settings().lease_seconds
    task_id = row["id"]
    attempt_count = row["attempt_count"]
    review_rounds = row["review_rounds"]
    execution_id: int | None = None
    agent_id: int | None = None
    event_payload = dict(payload)

    # ---- guards and execution side effects -------------------------------------------------
    if command == TaskCommand.PROMOTE and not dependencies_satisfied(conn, task_id):
        raise DependenciesNotMet(f"task {row['key']} has dependencies that are not DONE")

    if command in em.CREATED_BY:
        agent = _resolve_assignee(conn, command, actor, agent_key, row)
        run_budget = budgets.check_can_start(conn, row)
        agent_id = agent.id
        execution_id = _create_execution(conn, task_id, agent.id, lease, run_budget)
        event_payload |= {
            "agent": agent.key,
            "execution_status": em.INITIAL_STATE,
            "run_budget_usd": str(run_budget),
        }

    elif command in em.TRANSITIONS and state in EXECUTING_STATES:
        execution = _lock_active_execution(conn, task_id)
        execution_id, agent_id = execution["id"], execution["agent_id"]
        _check_execution_guards(command, actor, execution)
        new_status = em.next_execution_state(command, em.ExecutionState(execution["status"]))
        _update_execution(conn, execution["id"], new_status, command, payload, lease)
        event_payload |= {"agent": execution["agent_key"], "execution_status": new_status}

    if command == TaskCommand.SUBMIT:
        _record_delivery(conn, task_id, payload)

    budget_exhausted = False
    if command in (TaskCommand.FAIL, TaskCommand.TIME_OUT):
        budget_exhausted = budgets.task_budget_exhausted(conn, task_id, row["budget_usd"])
        attempt_count += 1
    elif command in (TaskCommand.REQUEST_CHANGES, TaskCommand.REJECT_MERGE):
        review_rounds += 1
    elif command == TaskCommand.RETRY:
        attempt_count = 0

    # ---- task update + event ------------------------------------------------------------
    target = next_state(
        command,
        state,
        actor.kind,
        attempts_after=attempt_count,
        max_retries=row["max_retries"],
        budget_exhausted=budget_exhausted,
    )
    if budget_exhausted:
        event_payload["budget_exhausted"] = True
    conn.execute(
        """UPDATE tasks SET state = %s, attempt_count = %s, review_rounds = %s, updated_at = now()
           WHERE id = %s""",
        (target, attempt_count, review_rounds, task_id),
    )
    append_event(
        conn,
        actor=actor,
        type=rule.event,
        project_id=row["project_id"],
        task_id=task_id,
        execution_id=execution_id,
        agent_id=agent_id,
        from_state=state,
        to_state=target,
        payload=event_payload | {"command": command},
    )

    # Consequences, logged after the transition that caused them.
    if command == TaskCommand.SUBMIT:
        review_store.request_reviews(conn, row, payload.get("commit"))
    elif state == TaskState.IN_REVIEW:  # approve / request_changes / escalate / cancel
        review_store.supersede_open_reviews(conn, task_id, reason=str(command))
    if command == TaskCommand.MERGE:
        for dependent_id in pending_dependents(conn, task_id):
            promote_if_ready(conn, dependent_id)


def _resolve_assignee(
    conn: Conn,
    command: TaskCommand,
    actor: Actor,
    agent_key: str | None,
    row: dict[str, Any],
) -> Agent:
    if command == TaskCommand.CLAIM:
        key = actor.name
    else:
        if not agent_key:
            raise InvalidInput("assign requires agent_key")
        key = agent_key
    # Lock the agent row: serializes concurrent claims/assignments for the same agent.
    if conn.execute("SELECT 1 FROM agents WHERE key = %s FOR UPDATE", (key,)).fetchone() is None:
        raise NotFound(f"agent {key} not found")
    agent = get_agent(conn, key)
    if not agent.enabled:
        raise AssignmentRejected(f"agent {key} is disabled")
    if agent.role != row["assigned_role"]:
        raise AssignmentRejected(
            f"agent {key} is a {agent.role}; task {row['key']} needs a {row['assigned_role']}"
        )
    if _active_executions(conn, agent_id=agent.id) >= agent.max_concurrent:
        raise AssignmentRejected(
            f"agent {key} is at capacity ({agent.max_concurrent} active executions)"
        )
    if agent.role == Role.DEVELOPER:
        # Serialize developer assignments so the global cap holds under concurrency. Lock order
        # everywhere: task row -> agent row -> this advisory lock.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (DEVELOPER_CAP_LOCK_ID,))
        cap = get_settings().max_concurrent_developers
        if _active_executions(conn, role=Role.DEVELOPER) >= cap:
            raise AssignmentRejected(f"global cap reached: {cap} developers already active")
    return agent


def _active_executions(conn: Conn, *, agent_id: int | None = None, role: Role | None = None) -> int:
    row = conn.execute(
        """SELECT count(*) AS n FROM executions e JOIN agents a ON a.id = e.agent_id
           WHERE e.status IN ('STARTING', 'RUNNING')
             AND (%(agent)s::bigint IS NULL OR e.agent_id = %(agent)s)
             AND (%(role)s::text IS NULL OR a.role = %(role)s)""",
        {"agent": agent_id, "role": role},
    ).fetchone()
    assert row is not None
    return int(row["n"])


def _create_execution(
    conn: Conn, task_id: int, agent_id: int, lease_seconds: int, run_budget: Decimal
) -> int:
    row = conn.execute(
        """INSERT INTO executions (task_id, agent_id, attempt, status, lease_expires_at,
                                   run_budget_usd)
           VALUES (%s, %s,
                   (SELECT count(*) + 1 FROM executions WHERE task_id = %s),
                   %s, now() + make_interval(secs => %s), %s)
           RETURNING id""",
        (task_id, agent_id, task_id, em.INITIAL_STATE, lease_seconds, run_budget),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _lock_active_execution(conn: Conn, task_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT e.*, a.key AS agent_key, (e.lease_expires_at <= now()) AS lease_expired
           FROM executions e JOIN agents a ON a.id = e.agent_id
           WHERE e.task_id = %s AND e.status IN ('STARTING', 'RUNNING')
           FOR UPDATE OF e""",
        (task_id,),
    ).fetchone()
    if row is None:  # would violate the task/execution invariant
        raise RuntimeError(f"task {task_id} is executing but has no active execution")
    return row


def _check_execution_guards(command: TaskCommand, actor: Actor, execution: dict[str, Any]) -> None:
    if actor.kind == ActorKind.AGENT:
        if actor.name != execution["agent_key"]:
            raise NotExecutionOwner(
                f"execution {execution['id']} belongs to {execution['agent_key']}, not {actor.name}"
            )
        if execution["lease_expired"]:
            raise LeaseExpired(f"lease on execution {execution['id']} has expired")
    if command == TaskCommand.TIME_OUT and not execution["lease_expired"]:
        raise LeaseNotExpired(f"lease on execution {execution['id']} is still live")


def _update_execution(
    conn: Conn,
    execution_id: int,
    status: em.ExecutionState,
    command: TaskCommand,
    payload: dict[str, Any],
    lease_seconds: int,
) -> None:
    terminal = status in em.TERMINAL_STATES
    cost_usd, tokens_in, tokens_out = budgets.parse_cost(payload)
    result = Jsonb(payload) if command in (TaskCommand.SUBMIT, TaskCommand.ASK_HUMAN) else None
    error = payload.get("error") if command == TaskCommand.FAIL else None
    if command == TaskCommand.TIME_OUT:
        error = "lease expired"
    conn.execute(
        """UPDATE executions SET
             status = %(status)s,
             ended_at = CASE WHEN %(terminal)s THEN now() END,
             lease_expires_at = CASE
               WHEN %(terminal)s THEN NULL
               ELSE now() + make_interval(secs => %(lease)s) END,
             result = COALESCE(%(result)s, result),
             error = COALESCE(%(error)s, error),
             cost_usd = COALESCE(%(cost_usd)s, cost_usd),
             cost_estimated = cost_estimated OR %(estimated)s,
             input_tokens = COALESCE(%(tokens_in)s, input_tokens),
             output_tokens = COALESCE(%(tokens_out)s, output_tokens),
             transcript_uri = COALESCE(%(transcript_uri)s, transcript_uri)
           WHERE id = %(id)s""",
        {
            "status": status,
            "terminal": terminal,
            "lease": lease_seconds,
            "result": result,
            "error": str(error) if error is not None else None,
            "cost_usd": cost_usd,
            "estimated": budgets.is_estimated(payload) and cost_usd is not None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "transcript_uri": payload.get("transcript_uri"),
            "id": execution_id,
        },
    )


def _record_delivery(conn: Conn, task_id: int, payload: dict[str, Any]) -> None:
    """On submit, remember the branch and pull request the worker produced."""
    branch, pr_url = payload.get("branch"), payload.get("pr_url")
    for name, value in (("branch", branch), ("pr_url", pr_url)):
        if value is not None and not isinstance(value, str):
            raise InvalidInput(f"{name} must be a string")
    commit = payload.get("commit")
    if commit is not None and not isinstance(commit, str):
        raise InvalidInput("commit must be a string")
    conn.execute(
        """UPDATE tasks SET branch = COALESCE(%s, branch),
                            pull_request_url = COALESCE(%s, pull_request_url),
                            head_sha = COALESCE(%s, head_sha),
                            ci_status = NULL, ci_sha = NULL, ci_details = NULL
           WHERE id = %s""",
        (branch, pr_url, commit, task_id),
    )


def record_execution_cost(
    conn: Conn, actor: Actor, execution_id: int, payload: dict[str, Any]
) -> bool:
    """A run that ended without reporting its cost (lease lost, worker killed): the money was
    spent, and budgets must see it. Not a state change. The first report wins, so a repeat cannot
    double-count. Returns whether anything was recorded."""
    cost_usd, tokens_in, tokens_out = budgets.parse_cost(payload)
    if cost_usd is None:
        raise InvalidInput("cost.usd is required")
    with conn.transaction():
        found = conn.execute(
            """SELECT e.task_id, e.agent_id, e.status, a.key AS agent_key
               FROM executions e JOIN agents a ON a.id = e.agent_id WHERE e.id = %s""",
            (execution_id,),
        ).fetchone()
        if found is None:
            raise NotFound(f"execution {execution_id} not found")
        task = conn.execute(
            "SELECT id, project_id FROM tasks WHERE id = %s FOR UPDATE", (found["task_id"],)
        ).fetchone()
        assert task is not None
        row = conn.execute(
            "SELECT status, cost_usd FROM executions WHERE id = %s FOR UPDATE", (execution_id,)
        ).fetchone()
        assert row is not None
        if actor.kind != ActorKind.AGENT or actor.name != found["agent_key"]:
            raise NotExecutionOwner(f"execution {execution_id} belongs to {found['agent_key']}")
        if row["status"] not in em.TERMINAL_STATES:
            raise InvalidInput(f"execution {execution_id} is still {row['status']}")
        if row["cost_usd"] is not None:
            return False
        conn.execute(
            """UPDATE executions SET cost_usd = %s, input_tokens = coalesce(input_tokens, %s),
                      output_tokens = coalesce(output_tokens, %s), cost_estimated = %s
               WHERE id = %s""",
            (cost_usd, tokens_in, tokens_out, budgets.is_estimated(payload), execution_id),
        )
        append_event(
            conn,
            actor=actor,
            type="EXECUTION_COST_RECORDED",
            project_id=task["project_id"],
            task_id=task["id"],
            execution_id=execution_id,
            agent_id=found["agent_id"],
            payload={"cost_usd": str(cost_usd), "estimated": budgets.is_estimated(payload)},
        )
    return True
