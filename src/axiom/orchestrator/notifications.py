"""Notifications (ADR-019): an outbox of things a human should hear about.

A notification is written in the same transaction as the event that causes it, so none is lost and
none exists without its cause. The sync service delivers the ones marked `push` as comments on
the project's inbox issue. A comment is **fixed text chosen here**: a sentence per reason, a task
key or plan number, and the time. Never anything a model wrote (a question, a plan, an error):
that text could carry injected instructions or a leaked value, and the repository may be public.
Replies on the issue are never read.
"""

import re
from datetime import datetime
from typing import Any

from axiom.state.db import Conn

# reason -> (pushed outside the dashboard, the sentence; {subject} is a task key or "plan N")
REASONS: dict[str, tuple[bool, str]] = {
    "credential_exposure": (True, "STOP: possible credential exposure on {subject}. Work on it has "
                                  "stopped. Check the dashboard before anything else."),
    "question": (True, "{subject} has a question for you."),
    "escalated": (True, "{subject} needs your decision."),
    "pr_ready": (True, "{subject} passed review and CI: its pull request is ready to merge."),
    "plan_ready": (True, "{subject} is ready for your approval."),
    "reminder": (True, "Reminder: {subject} is still waiting for you."),
    "schedule_refused": (True, "{subject} asked for nothing this time: the day's budget is nearly "
                               "spent."),
}  # fmt: skip
FIRST = "credential_exposure"  # always delivered before anything else
_TASK_KEY = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


def notify(
    conn: Conn,
    reason: str,
    *,
    project_id: int,
    dedup_key: str,
    task_id: int | None = None,
    plan_id: int | None = None,
    timer_id: int | None = None,
) -> bool:
    """Record one notification inside the caller's transaction. False if it was already recorded."""
    push, _ = REASONS[reason]
    row = conn.execute(
        """INSERT INTO notifications
             (project_id, reason, task_id, plan_id, timer_id, dedup_key, push)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (dedup_key) DO NOTHING RETURNING id""",
        (project_id, reason, task_id, plan_id, timer_id, dedup_key, push),
    ).fetchone()
    return row is not None


def subject_of(n: dict[str, Any]) -> str:
    """A task key, "plan N" or "schedule N": checked, so nothing else can reach a comment."""
    key = n.get("task_key")
    if key is not None:
        if not isinstance(key, str) or not _TASK_KEY.match(key):
            raise ValueError("not a task key")
        return key
    if n.get("plan_id") is not None:
        return f"plan {int(n['plan_id'])}"
    if n.get("timer_id") is not None:
        return f"schedule {int(n['timer_id'])}"
    raise ValueError("a notification needs a subject")


def comment_text(n: dict[str, Any]) -> str:
    _, sentence = REASONS[n["reason"]]
    when: datetime = n["created_at"]
    return f"{sentence.format(subject=subject_of(n))}\n\n_{when:%Y-%m-%d %H:%M} UTC · Axiom_"


def list_notifications(
    conn: Conn, *, undelivered: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT n.*, t.key AS task_key, p.key AS project_key FROM notifications n
           JOIN projects p ON p.id = n.project_id LEFT JOIN tasks t ON t.id = n.task_id
           WHERE NOT %(u)s OR (n.push AND n.delivered_at IS NULL)
           ORDER BY n.id DESC LIMIT %(limit)s""",
        {"u": undelivered, "limit": limit},
    ).fetchall()


def to_deliver(conn: Conn, limit: int = 20) -> list[dict[str, Any]]:
    """Pushed and not yet delivered: a credential exposure first, then oldest first."""
    return conn.execute(
        """SELECT n.*, t.key AS task_key, p.key AS project_key, p.repo_url, p.inbox_issue
           FROM notifications n JOIN projects p ON p.id = n.project_id
           LEFT JOIN tasks t ON t.id = n.task_id
           WHERE n.push AND n.delivered_at IS NULL
           ORDER BY (n.reason = %s) DESC, n.id LIMIT %s""",
        (FIRST, limit),
    ).fetchall()


def mark_delivered(conn: Conn, notification_id: int) -> None:
    conn.execute(
        """UPDATE notifications SET delivered_at = now(), attempts = attempts + 1,
                  delivery_error = NULL WHERE id = %s""",
        (notification_id,),
    )


def mark_failed(conn: Conn, notification_id: int, error: str) -> None:
    conn.execute(
        "UPDATE notifications SET attempts = attempts + 1, delivery_error = %s WHERE id = %s",
        (error[:500], notification_id),
    )


def set_inbox_issue(conn: Conn, project_id: int, number: int) -> None:
    conn.execute("UPDATE projects SET inbox_issue = %s WHERE id = %s", (number, project_id))
