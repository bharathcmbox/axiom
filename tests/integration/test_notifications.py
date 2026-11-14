"""Phase 5 T5: notifications (ADR-019). One per cause, in the cause's transaction; delivered as
fixed text to the inbox issue, a credential exposure first; replies never read."""

from decimal import Decimal
from typing import Any

import pytest

from axiom.config import get_settings
from axiom.domain.task_machine import ActorNotPermitted
from axiom.orchestrator import plans, timers
from axiom.registry.projects import set_test_paths
from axiom.state.db import Conn
from axiom.sync.inbox import INBOX_TITLE, GitHubWriteError, deliver
from tests.integration.helpers import DEV1, HUMAN, C, S, do, drive_to, escalated
from tests.integration.test_plans import EM, GOOD


def _all(db: Conn) -> list[dict[str, Any]]:
    return db.execute("SELECT * FROM notifications ORDER BY id").fetchall()


def _reasons(db: Conn) -> list[str]:
    return [n["reason"] for n in _all(db)]


@pytest.fixture
def repo(org: Conn) -> Conn:
    org.execute("UPDATE projects SET repo_url = 'https://github.com/o/r.git' WHERE key = 'AX'")
    return org


class FakeWriter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.issues: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str, int, str]] = []

    def open_issue(self, owner: str, repo: str, title: str, body: str) -> int:
        self.issues.append((owner, repo, title))
        return 41 + len(self.issues)

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        if self.fail:
            raise GitHubWriteError("GitHub POST /comments -> 502")
        self.comments.append((owner, repo, number, body))


# ---- one notification per cause --------------------------------------------------------------


def test_a_question_notifies_you_once(org: Conn) -> None:
    drive_to(org, S.WAITING_FOR_HUMAN)
    [n] = _all(org)
    assert (n["reason"], n["push"], n["delivered_at"]) == ("question", True, None)


def test_a_refused_command_notifies_nobody(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    with pytest.raises(ActorNotPermitted):
        do(org, key, C.ASK_HUMAN, HUMAN)  # only the agent asks
    assert _all(org) == []


def test_an_escalation_notifies_you(org: Conn) -> None:
    escalated(org)
    assert _reasons(org) == ["escalated"]


def test_a_pr_ready_to_merge_notifies_you(org: Conn) -> None:
    drive_to(org, S.AWAITING_MERGE)
    assert _reasons(org) == ["pr_ready"]


def test_a_credential_exposure_notifies_you(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    do(org, key, C.FAIL, payload={"error": "verification failed", "credential_exposure": True})
    assert _reasons(org) == ["credential_exposure"]


def test_an_ordinary_failure_doesnt(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    do(org, key, C.FAIL)
    assert _all(org) == []


def test_a_proposed_plan_notifies_you(org: Conn) -> None:
    set_test_paths(org, HUMAN, "AX", ["agent/tests/**"])
    plan_id = int(plans.request_plan(org, HUMAN, project_key="AX", specification="Build it.",
                                     budget_usd=Decimal(10))["id"])  # fmt: skip
    plans.claim_plan(org, EM)
    plans.submit_plan(org, EM, plan_id, GOOD, {"cost": {"usd": "0.40"}})
    [n] = _all(org)
    assert (n["reason"], n["plan_id"]) == ("plan_ready", plan_id)


def test_a_reminder_is_sent_once(org: Conn) -> None:
    drive_to(org, S.WAITING_FOR_HUMAN)
    [follow_up] = timers.list_timers(org, "PENDING")
    org.execute("UPDATE timers SET due_at = now() - interval '1 second' WHERE id = %s",
                (follow_up["id"],))  # fmt: skip
    timers.fire_due_timers(org)
    timers.fire_due_timers(org)
    assert _reasons(org) == ["question", "reminder"]


def test_a_refused_schedule_notifies_you(org: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal("0.40"))
    timer = timers.create_schedule(org, HUMAN, project_key="AX", specification="x",
                                   budget_usd=Decimal(5), every_hours=24)  # fmt: skip
    org.execute("UPDATE timers SET due_at = now() - interval '1 second' WHERE id = %s",
                (timer["id"],))  # fmt: skip
    timers.fire_due_timers(org)
    [n] = _all(org)
    assert (n["reason"], n["timer_id"]) == ("schedule_refused", timer["id"])


def test_asking_again_later_is_a_new_notification(org: Conn) -> None:
    key = drive_to(org, S.WAITING_FOR_HUMAN)
    do(org, key, C.ANSWER)
    do(org, key, C.CLAIM, DEV1, agent_key="DEV-01")
    do(org, key, C.START)
    do(org, key, C.ASK_HUMAN)
    assert _reasons(org) == ["question", "question"]


# ---- delivery ----------------------------------------------------------------------------------


def test_delivery_opens_the_inbox_once_and_posts_fixed_text(repo: Conn) -> None:
    drive_to(repo, S.WAITING_FOR_HUMAN)
    drive_to(repo, S.AWAITING_MERGE)
    writer = FakeWriter()
    results = deliver(repo, writer)
    assert list(results.values()) == ["delivered", "delivered"]
    assert writer.issues == [("o", "r", INBOX_TITLE)]
    assert [c[2] for c in writer.comments] == [42, 42]
    assert "AX-001 has a question for you." in writer.comments[0][3]
    assert "Which database?" not in writer.comments[0][3]  # the agent's question stays home
    assert deliver(repo, writer) == {}  # nothing twice
    project = repo.execute("SELECT inbox_issue FROM projects WHERE key = 'AX'").fetchone()
    assert project is not None and project["inbox_issue"] == 42


def test_a_credential_exposure_is_delivered_first(repo: Conn) -> None:
    drive_to(repo, S.AWAITING_MERGE)
    key = drive_to(repo, S.IN_PROGRESS)
    do(repo, key, C.FAIL, payload={"error": "leak", "credential_exposure": True})
    writer = FakeWriter()
    deliver(repo, writer)
    assert writer.comments[0][3].startswith("STOP: possible credential exposure on AX-002")


def test_a_failed_delivery_is_recorded_and_retried(repo: Conn) -> None:
    drive_to(repo, S.WAITING_FOR_HUMAN)
    [result] = deliver(repo, FakeWriter(fail=True)).values()
    assert result.startswith("error")
    [n] = _all(repo)
    assert (n["delivered_at"], n["attempts"]) == (None, 1) and "502" in n["delivery_error"]
    deliver(repo, FakeWriter())
    [n] = _all(repo)
    assert n["delivered_at"] is not None and n["delivery_error"] is None


def test_the_writer_cannot_read_replies() -> None:
    """Structural: the inbox writer has no method that reads anything from GitHub."""
    from axiom.sync.inbox import GitHubInboxWriter

    public = {m for m in dir(GitHubInboxWriter) if not m.startswith("_")}
    assert public == {"open_issue", "comment", "api", "timeout"}
