"""What a developer needs to know before starting another round on a task.

Read from the task's event log (the single source of truth): everything that happened since the
last submission. That covers review findings and CI failures (TASK_REOPENED), a human closing
the loop on GitHub (reject_merge), an escalation reason, and question/answer pairs.
"""

from typing import Any


def feedback_from_events(events: list[dict[str, Any]]) -> list[str]:
    last_submit = max(
        (i for i, e in enumerate(events) if e["type"] == "TASK_SUBMITTED"), default=-1
    )
    feedback: list[str] = []
    for e in events[last_submit + 1 :]:
        p = e.get("payload") or {}
        if e["type"] == "TASK_REOPENED":
            items = p.get("feedback") or []
            if not items and p.get("findings"):
                items = [str(f) for f in p["findings"]]
            if not items:
                items = [
                    f"changes requested by {e['actor']}"
                    + (f": {p['reason']}" if p.get("reason") else "")
                ]
            feedback.extend(items)
        elif e["type"] == "TASK_ESCALATED" and p.get("reason"):
            feedback.append(f"escalated to a human: {p['reason']}")
        elif e["type"] == "HUMAN_INPUT_REQUESTED" and p.get("question"):
            feedback.append(f"you asked: {p['question']}")
        elif e["type"] == "HUMAN_INPUT_PROVIDED" and p.get("answer"):
            feedback.append(f"human answer: {p['answer']}")
    return feedback
