"""Low-level review bookkeeping used inside task-command transactions (no task transitions).

Kept separate from `reviews.py` (claims, submissions, aggregation) so that the command engine
can create and supersede reviews without an import cycle.
"""

from typing import Any

from axiom.config import get_settings
from axiom.domain.review_machine import OPEN, ReviewCommand, ReviewState, next_review_state
from axiom.domain.roles import Actor, ActorKind
from axiom.events.log import append_event
from axiom.state.db import Conn

SYSTEM_REVIEWS = Actor(ActorKind.SYSTEM, "reviews")


def review_event(
    conn: Conn,
    actor: Actor,
    review: dict[str, Any],
    event_type: str,
    *,
    to_state: str,
    payload: dict[str, Any] | None = None,
    agent_id: int | None = None,
) -> None:
    """Review events: linked to task and review, but never move the task (from/to stay NULL)."""
    task = conn.execute(
        "SELECT project_id FROM tasks WHERE id = %s", (review["task_id"],)
    ).fetchone()
    append_event(
        conn,
        actor=actor,
        type=event_type,
        project_id=task["project_id"] if task else None,
        task_id=review["task_id"],
        agent_id=agent_id,
        review_id=review["id"],
        payload={
            "role": review["role"],
            "round": review["round"],
            "review_from": review["status"],
            "review_to": to_state,
        }
        | (payload or {}),
    )


def request_reviews(conn: Conn, task_row: dict[str, Any], head_sha: str | None) -> list[int]:
    """Create one PENDING review per required role for the task's next round."""
    round_ = task_row["review_rounds"] + 1
    ids = []
    for role in get_settings().required_review_roles:
        review = conn.execute(
            """INSERT INTO reviews (task_id, round, role, status, head_sha)
               VALUES (%s, %s, %s, 'PENDING', %s) RETURNING *""",
            (task_row["id"], round_, role, head_sha),
        ).fetchone()
        assert review is not None
        review_event(
            conn,
            SYSTEM_REVIEWS,
            review | {"status": None},
            "REVIEW_REQUESTED",
            to_state=ReviewState.PENDING,
            payload={"head_sha": head_sha},
        )
        ids.append(review["id"])
    return ids


def supersede_open_reviews(conn: Conn, task_id: int, reason: str) -> int:
    rows = conn.execute(
        "SELECT * FROM reviews WHERE task_id = %s AND status = ANY(%s) ORDER BY id FOR UPDATE",
        (task_id, [str(s) for s in OPEN]),
    ).fetchall()
    for review in rows:
        target = next_review_state(
            ReviewCommand.SUPERSEDE, ReviewState(review["status"]), ActorKind.SYSTEM
        )
        conn.execute(
            """UPDATE reviews SET status = %s, lease_expires_at = NULL, completed_at = now()
               WHERE id = %s""",
            (target, review["id"]),
        )
        review_event(
            conn,
            SYSTEM_REVIEWS,
            review,
            "REVIEW_SUPERSEDED",
            to_state=target,
            payload={"reason": reason},
        )
    return len(rows)
