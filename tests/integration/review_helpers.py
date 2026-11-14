"""Submit reviews in tests without spelling out the role's checklist each time (Phase 5 T2).

Tests written before checklists existed submit `{"verdict", "summary", "findings"}`. This fills in
a complete checklist for the review's role, marking an item `finding` when there are findings, so
those tests keep testing what they were written for. Checklist rules have their own tests.
"""

from typing import Any

from axiom.contracts import REVIEW_CHECKLISTS
from axiom.domain.roles import Actor
from axiom.orchestrator import reviews
from axiom.state.db import Conn


def with_checklist(contract: dict[str, Any], role: str) -> dict[str, Any]:
    if "checklist" in contract or role not in REVIEW_CHECKLISTS:
        return contract
    items = list(REVIEW_CHECKLISTS[role])
    flagged = items[0] if contract.get("findings") else None
    checklist = {
        i: {"result": "finding" if i == flagged else "ok", "note": f"looked at {i}"} for i in items
    }
    return contract | {"checklist": checklist}


def role_of(conn: Conn, review_id: int) -> str:
    row = conn.execute("SELECT role FROM reviews WHERE id = %s", (review_id,)).fetchone()
    return str(row["role"]) if row else ""


def submit_review(
    conn: Conn,
    actor: Actor,
    review_id: int,
    contract: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    return reviews.submit_review(
        conn, actor, review_id, with_checklist(contract, role_of(conn, review_id)), payload
    )
