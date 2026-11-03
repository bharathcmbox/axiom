"""The aggregator: turns reviews and CI into one task decision (ADR-009). Deterministic code.

Called (inside the caller's transaction, with the task row locked) whenever a review finishes
or the CI status changes. Decision table, evaluated in order, only while the task is IN_REVIEW
and only on the current round's reviews:

| Condition                                              | Command          |
|--------------------------------------------------------|------------------|
| any review ERROR                                       | escalate         |
| any review FAIL, or CI failure (for the head commit)   | request_changes  |
|   ...but this round is the last allowed round          | escalate         |
| all reviews PASS and (CI not required or CI success)   | approve          |
| otherwise (reviews or CI still pending)                | wait             |
"""

from typing import Any

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.events.log import append_event
from axiom.orchestrator.commands import apply_locked
from axiom.state.db import Conn

AGGREGATOR = Actor(ActorKind.SYSTEM, "aggregator")
CI_STATUSES = ("pending", "success", "failure")


def current_reviews(conn: Conn, task: dict[str, Any]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM reviews WHERE task_id = %s AND round = %s ORDER BY role",
        (task["id"], task["review_rounds"] + 1),
    ).fetchall()


def decide(
    task: dict[str, Any], reviews: list[dict[str, Any]], require_ci: bool
) -> tuple[str, dict[str, Any]]:
    """Pure decision: ("wait" | "approve" | "request_changes" | "escalate", payload)."""
    statuses = {r["role"]: r["status"] for r in reviews}
    if any(s == "ERROR" for s in statuses.values()):
        failed = [r["role"] for r in reviews if r["status"] == "ERROR"]
        return "escalate", {"reason": f"review could not be completed: {', '.join(failed)}"}

    ci_current = task.get("ci_sha") is not None and task.get("ci_sha") == task.get("head_sha")
    ci_failed = require_ci and ci_current and task.get("ci_status") == "failure"
    failed_reviews = [r for r in reviews if r["status"] == "FAIL"]
    if failed_reviews or ci_failed:
        feedback: list[str] = []
        findings: list[dict[str, Any]] = []
        for r in failed_reviews:
            contract = r["contract"] or {}
            feedback.append(f"{r['role']}: {contract.get('summary', 'FAIL')}")
            for f in contract.get("findings", []):
                findings.append({"reviewer": r["role"]} | f)
                where = f"{f.get('file') or '?'}:{f.get('line') or '?'}"
                feedback.append(f"[{f['severity']}] {where} {f['issue']} → {f['required_change']}")
        if ci_failed:
            details = task.get("ci_details") or {}
            feedback.append(
                f"CI failed for {task['ci_sha'][:7]}: {details.get('summary', 'see CI log')}"
            )
        payload = {"feedback": feedback, "findings": findings, "ci_failed": ci_failed}
        if task["review_rounds"] + 1 >= get_settings().max_review_rounds:
            return "escalate", payload | {
                "reason": f"changes still requested after {task['review_rounds'] + 1} review rounds"
            }
        return "request_changes", payload

    required = set(get_settings().required_review_roles)
    all_passed = required <= {role for role, s in statuses.items() if s == "PASS"}
    ci_ok = not require_ci or (ci_current and task.get("ci_status") == "success")
    if all_passed and ci_ok:
        return "approve", {"reviews": sorted(required), "ci": task.get("ci_status")}
    return "wait", {}


def aggregate(conn: Conn, task: dict[str, Any]) -> str:
    """Apply the decision for a locked task row. Returns the decision taken."""
    if task["state"] != TaskState.IN_REVIEW:
        return "wait"
    project = conn.execute(
        "SELECT require_ci FROM projects WHERE id = %s", (task["project_id"],)
    ).fetchone()
    decision, payload = decide(
        task, current_reviews(conn, task), bool(project and project["require_ci"])
    )
    if decision != "wait":
        apply_locked(conn, task, TaskCommand(decision), AGGREGATOR, payload=payload)
    return decision


def record_ci(
    conn: Conn, actor: Actor, task_key: str, *, sha: str, status: str, details: dict[str, Any]
) -> str:
    """Store the CI conclusion for a commit, then re-aggregate. Called by the GitHub sync."""
    if status not in CI_STATUSES:
        raise ValueError(f"ci status must be one of {CI_STATUSES}")
    from psycopg.types.json import Jsonb

    from axiom.orchestrator.commands import lock_task

    with conn.transaction():
        task = lock_task(conn, task_key)
        if (task["ci_sha"], task["ci_status"]) == (sha, status):
            return "unchanged"
        conn.execute(
            "UPDATE tasks SET ci_status = %s, ci_sha = %s, ci_details = %s WHERE id = %s",
            (status, sha, Jsonb(details), task["id"]),
        )
        append_event(
            conn,
            actor=actor,
            type="CI_STATUS_CHANGED",
            project_id=task["project_id"],
            task_id=task["id"],
            payload={"sha": sha, "status": status, "current": sha == task["head_sha"]} | details,
        )
        task = lock_task(conn, task_key)
        return aggregate(conn, task)
