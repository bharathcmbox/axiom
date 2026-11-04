"""Phase 3 T5: what a developer is told before a rework round, read from the event log."""

from typing import Any

from axiom.worker.feedback import feedback_from_events


def _e(type_: str, actor: str = "system:aggregator", **payload: Any) -> dict[str, Any]:
    return {"type": type_, "actor": actor, "payload": payload}


def test_only_events_since_the_last_submission_count() -> None:
    events = [
        _e("TASK_SUBMITTED"),
        _e("TASK_REOPENED", feedback=["old round: fix A"]),
        _e("TASK_SUBMITTED"),
        _e(
            "TASK_REOPENED",
            feedback=[
                "code_reviewer: off by one",
                "[high] quota.py:3 used < limit → used <= limit",
            ],
        ),
        _e("TASK_ASSIGNED", actor="agent:DEV-01"),
    ]
    assert feedback_from_events(events) == [
        "code_reviewer: off by one", "[high] quota.py:3 used < limit → used <= limit",
    ]  # fmt: skip


def test_ci_failure_and_human_rejection() -> None:
    events = [
        _e("TASK_SUBMITTED"),
        _e("TASK_REOPENED", feedback=["CI failed for abc1234: 2 failed"]),
    ]
    assert feedback_from_events(events) == ["CI failed for abc1234: 2 failed"]
    human = [_e("TASK_SUBMITTED"), _e("TASK_REOPENED", actor="human:github-bharathcmbox")]
    assert feedback_from_events(human) == ["changes requested by human:github-bharathcmbox"]


def test_questions_answers_and_escalations() -> None:
    events = [
        _e("TASK_SUBMITTED"),
        _e("TASK_ESCALATED", reason="changes still requested after 3 review rounds"),
        _e("HUMAN_INPUT_PROVIDED", actor="human:bharath", answer="Keep the old API too"),
    ]
    assert feedback_from_events(events) == [
        "escalated to a human: changes still requested after 3 review rounds",
        "human answer: Keep the old API too",
    ]


def test_first_attempt_has_no_feedback() -> None:
    assert feedback_from_events([_e("TASK_CREATED"), _e("TASK_READY")]) == []
