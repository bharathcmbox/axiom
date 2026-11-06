"""Reviewer work: claim a review (with a lease), heartbeat, submit a verdict, report a failed run.

Lock order matches the rest of Axiom: task row first, then the review row. Every change writes
a review event; finishing a review re-runs the aggregator in the same transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from axiom.config import get_settings
from axiom.contracts import InvalidResult, parse_review
from axiom.domain.review_machine import ReviewCommand, ReviewState, next_review_state
from axiom.domain.roles import Actor, ActorKind
from axiom.errors import (
    AssignmentRejected,
    Forbidden,
    InvalidInput,
    LeaseExpired,
    NotExecutionOwner,
    NotFound,
)
from axiom.orchestrator import budgets
from axiom.orchestrator.aggregator import aggregate
from axiom.orchestrator.review_store import review_event
from axiom.registry.agents import get_agent
from axiom.state.db import Conn

REVIEW_ROLES = ("code_reviewer", "security_reviewer")
REAPER = Actor(ActorKind.SYSTEM, "reaper")


@dataclass(frozen=True)
class ReviewAssignment:
    """Everything a reviewer worker needs, returned by a claim."""

    review_id: int
    role: str
    round: int
    head_sha: str | None
    lease_expires_at: datetime
    run_budget_usd: Decimal
    task: dict[str, Any]
    developer_result: dict[str, Any] | None


def _review_for_update(conn: Conn, review_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Lock task then review (global lock order). Returns (task, review)."""
    head = conn.execute("SELECT task_id FROM reviews WHERE id = %s", (review_id,)).fetchone()
    if head is None:
        raise NotFound(f"review {review_id} not found")
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = %s FOR UPDATE", (head["task_id"],)
    ).fetchone()
    review = conn.execute(
        """SELECT r.*, a.key AS reviewer_key, (r.lease_expires_at <= now()) AS lease_expired
           FROM reviews r LEFT JOIN agents a ON a.id = r.reviewer_agent_id
           WHERE r.id = %s FOR UPDATE OF r""",
        (review_id,),
    ).fetchone()
    assert task is not None and review is not None
    return task, review


def _require_owner(actor: Actor, review: dict[str, Any]) -> None:
    if actor.kind != ActorKind.AGENT or actor.name != review["reviewer_key"]:
        raise NotExecutionOwner(f"review {review['id']} is held by {review['reviewer_key']}")
    if review["lease_expired"]:
        raise LeaseExpired(f"lease on review {review['id']} has expired")


def claim_review(
    conn: Conn, actor: Actor, *, lease_seconds: int | None = None
) -> ReviewAssignment | None:
    """A reviewer agent claims the oldest PENDING review for its role, or gets None."""
    if actor.kind != ActorKind.AGENT:
        raise Forbidden("only agents claim reviews")
    agent = get_agent(conn, actor.name)
    if agent.role not in REVIEW_ROLES:
        raise Forbidden(f"agent {agent.key} is a {agent.role}, not a reviewer")
    lease = lease_seconds or get_settings().review_lease_seconds
    with conn.transaction():
        row = conn.execute(
            """SELECT r.id AS review_id, t.id AS task_id FROM reviews r
               JOIN tasks t ON t.id = r.task_id
               WHERE r.status = 'PENDING' AND r.role = %s AND t.state = 'IN_REVIEW'
               ORDER BY r.created_at, r.id LIMIT 1
               FOR UPDATE OF t, r SKIP LOCKED""",
            (agent.role,),
        ).fetchone()
        if row is None:
            return None
        if not agent.enabled:
            raise AssignmentRejected(f"agent {agent.key} is disabled")
        conn.execute("SELECT 1 FROM agents WHERE id = %s FOR UPDATE", (agent.id,))
        held = conn.execute(
            """SELECT (SELECT count(*) FROM reviews WHERE reviewer_agent_id = %(a)s
                         AND status = 'CLAIMED')
                    + (SELECT count(*) FROM executions WHERE agent_id = %(a)s
                         AND status IN ('STARTING', 'RUNNING')) AS n""",
            {"a": agent.id},
        ).fetchone()
        if held is not None and held["n"] >= agent.max_concurrent:
            raise AssignmentRejected(f"agent {agent.key} is at capacity")
        cap = get_settings().daily_budget_usd
        if budgets.daily_spent(conn) >= cap:
            raise AssignmentRejected(f"daily budget of ${cap} is spent")
        task, review = _review_for_update(conn, row["review_id"])
        target = next_review_state(ReviewCommand.CLAIM, ReviewState(review["status"]), actor.kind)
        updated = conn.execute(
            """UPDATE reviews SET status = %s, reviewer_agent_id = %s,
                      lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s RETURNING lease_expires_at""",
            (target, agent.id, lease, review["id"]),
        ).fetchone()
        assert updated is not None
        day_left = cap - budgets.daily_spent(conn)
        run_budget = max(min(get_settings().review_budget_usd, day_left), Decimal("0"))
        review_event(
            conn,
            actor,
            review,
            "REVIEW_CLAIMED",
            to_state=target,
            agent_id=agent.id,
            payload={"run_budget_usd": str(run_budget)},
        )
        submitted = conn.execute(
            """SELECT result FROM executions WHERE task_id = %s AND status = 'COMPLETED'
               ORDER BY id DESC LIMIT 1""",
            (task["id"],),
        ).fetchone()
        return ReviewAssignment(
            review_id=review["id"],
            role=review["role"],
            round=review["round"],
            head_sha=review["head_sha"],
            lease_expires_at=updated["lease_expires_at"],
            run_budget_usd=run_budget,
            task={
                k: task[k]
                for k in (
                    "key",
                    "title",
                    "specification",
                    "acceptance_criteria",
                    "allowed_paths",
                    "branch",
                    "pull_request_url",
                    "project_id",
                )
            },
            developer_result=submitted["result"] if submitted else None,
        )


def review_heartbeat(
    conn: Conn, actor: Actor, review_id: int, *, lease_seconds: int | None = None
) -> datetime:
    lease = lease_seconds or get_settings().review_lease_seconds
    with conn.transaction():
        _task, review = _review_for_update(conn, review_id)
        if review["status"] != ReviewState.CLAIMED:
            raise InvalidInput(f"review {review_id} is {review['status']}, not CLAIMED")
        _require_owner(actor, review)
        row = conn.execute(
            """UPDATE reviews SET lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s RETURNING lease_expires_at""",
            (lease, review_id),
        ).fetchone()
        assert row is not None
        expires: datetime = row["lease_expires_at"]
        return expires


def submit_review(
    conn: Conn,
    actor: Actor,
    review_id: int,
    contract: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    """Record a verdict. Returns the aggregator's decision for the task."""
    try:
        parsed = parse_review(contract)
    except InvalidResult as e:
        raise InvalidInput(f"invalid review contract: {e}") from None
    cost_usd, tokens_in, tokens_out = budgets.parse_cost(payload or {})
    with conn.transaction():
        task, review = _review_for_update(conn, review_id)
        _require_owner(actor, review)
        if review["status"] == ReviewState.SUPERSEDED:
            return _record_late_cost(conn, actor, review, cost_usd, tokens_in, tokens_out,
                                     late_verdict=parsed.verdict)  # fmt: skip
        verdict = parsed.verdict
        override = None
        if verdict == "PASS" and parsed.blocking:  # ADR-009 consistency rule
            verdict, override = "FAIL", "PASS with high/critical findings treated as FAIL"
        target = next_review_state(
            ReviewCommand.SUBMIT, ReviewState(review["status"]), actor.kind, verdict=verdict
        )
        conn.execute(
            """UPDATE reviews SET status = %s, verdict = %s, contract = %s, lease_expires_at = NULL,
                      completed_at = now(), cost_usd = %s, input_tokens = %s, output_tokens = %s
               WHERE id = %s""",
            (
                target,
                verdict,
                Jsonb(parsed.model_dump(mode="json")),
                cost_usd,
                tokens_in,
                tokens_out,
                review_id,
            ),
        )
        event_payload = {
            "verdict": verdict,
            "findings": len(parsed.findings),
            "blocking": len(parsed.blocking),
            "policy_override": override,
            "cost_usd": str(cost_usd) if cost_usd is not None else None,
        }
        review_event(
            conn,
            actor,
            review,
            "REVIEW_SUBMITTED",
            to_state=target,
            agent_id=review["reviewer_agent_id"],
            payload=event_payload,
        )
        return aggregate(conn, task)


def fail_review_run(
    conn: Conn, actor: Actor, review_id: int, error: str, payload: dict[str, Any] | None = None
) -> str:
    """The reviewer's run crashed or produced no valid contract: retry, or ERROR after 3."""
    cost_usd, tokens_in, tokens_out = budgets.parse_cost(payload or {})  # failures cost money too
    with conn.transaction():
        task, review = _review_for_update(conn, review_id)
        _require_owner(actor, review)
        if review["status"] == ReviewState.SUPERSEDED:
            return _record_late_cost(conn, actor, review, cost_usd, tokens_in, tokens_out,
                                     late_verdict=None)  # fmt: skip
        return _retry_or_error(
            conn, actor, task, review, ReviewCommand.FAIL_RUN, "REVIEW_RUN_FAILED", error, cost_usd
        )


def _record_late_cost(
    conn: Conn,
    actor: Actor,
    review: dict[str, Any],
    cost_usd: Decimal | None,
    tokens_in: int | None,
    tokens_out: int | None,
    late_verdict: str | None,
) -> str:
    """A superseded review's run may finish anyway: its verdict no longer counts, but the money
    was spent and budgets must see it. The first report wins, so a repeat cannot double-count."""
    conn.execute(
        """UPDATE reviews SET cost_usd = coalesce(cost_usd, %s),
                  input_tokens = coalesce(input_tokens, %s),
                  output_tokens = coalesce(output_tokens, %s)
           WHERE id = %s""",
        (cost_usd, tokens_in, tokens_out, review["id"]),
    )
    review_event(
        conn,
        actor,
        review,
        "REVIEW_COST_RECORDED",
        to_state=review["status"],
        agent_id=review["reviewer_agent_id"],
        payload={
            "cost_usd": str(cost_usd) if cost_usd is not None else None,
            "late_verdict": late_verdict,
        },
    )
    return "superseded"


def _retry_or_error(
    conn: Conn,
    actor: Actor,
    task: dict[str, Any],
    review: dict[str, Any],
    command: ReviewCommand,
    event: str,
    error: str,
    cost_usd: Decimal | None,
) -> str:
    attempts = review["attempt_count"] + 1
    target = next_review_state(
        command, ReviewState(review["status"]), actor.kind, attempts_after=attempts
    )
    conn.execute(
        """UPDATE reviews SET status = %s, attempt_count = %s, lease_expires_at = NULL,
                  error = %s, cost_usd = coalesce(cost_usd, 0) + coalesce(%s, 0),
                  completed_at = CASE WHEN %s = 'ERROR' THEN now() END
           WHERE id = %s""",
        (target, attempts, error[:2000], cost_usd, target, review["id"]),
    )
    review_event(
        conn,
        actor,
        review,
        event,
        to_state=target,
        agent_id=review["reviewer_agent_id"],
        payload={"error": error[:500], "attempts": attempts},
    )
    return aggregate(conn, task) if target == ReviewState.ERROR else "wait"


def expire_review_leases(conn: Conn) -> list[int]:
    """Reaper: time out CLAIMED reviews whose lease expired. Returns their ids."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM reviews WHERE status = 'CLAIMED' AND lease_expires_at <= now() ORDER BY id"
    ).fetchall()]  # fmt: skip
    done = []
    for review_id in ids:
        with conn.transaction():
            task, review = _review_for_update(conn, review_id)
            if review["status"] != ReviewState.CLAIMED or not review["lease_expired"]:
                continue  # renewed or finished meanwhile
            _retry_or_error(
                conn,
                REAPER,
                task,
                review,
                ReviewCommand.TIME_OUT,
                "REVIEW_TIMED_OUT",
                "lease expired",
                None,
            )
            done.append(review_id)
    return done


def list_reviews(conn: Conn, task_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT r.*, a.key AS reviewer_key FROM reviews r
           LEFT JOIN agents a ON a.id = r.reviewer_agent_id
           WHERE r.task_id = %s ORDER BY r.round, r.role""",
        (task_id,),
    ).fetchall()
