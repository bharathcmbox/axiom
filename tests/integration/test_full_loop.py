"""Phase 3 T7: every role together. A buggy change is caught by an independent reviewer, sent
back, reworked on the same PR, passes review, passes CI, and is merged by a human on GitHub.

Real: Axiom API over HTTP, database, engine, aggregator, git clones/pushes/diffs, the sync
service. Fake: the models (FakeRuntime, FakeReviewer), GitHub's API (FakeReader, FakePrOpener).
"""

from pathlib import Path
from typing import Any

from axiom.domain.task_machine import TaskState
from axiom.orchestrator.tasks import get_task
from axiom.runtimes.fake import FakeRuntime
from axiom.runtimes.fake_review import FakeReviewer, ReviewScenario
from axiom.state.db import Conn
from axiom.sync.service import sync_once
from tests.integration.helpers import events_for
from tests.integration.test_review_worker import reviewer
from tests.integration.workers_shared import FakePrOpener, make_project, make_task, make_worker


class FakeReader:
    """GitHub as the sync sees it: one PR, CI runs per commit, and its merge state."""

    def __init__(self) -> None:
        self.ci: dict[str, str] = {}
        self.merged = False

    def pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        if self.merged:
            return {"state": "closed", "merged": True, "merged_by": {"login": "bharathcmbox"}}
        return {"state": "open", "merged": False}

    def reviews(self, *a: Any) -> list[dict[str, Any]]:
        return []

    def comments(self, *a: Any) -> list[dict[str, Any]]:
        return []

    def workflow_runs(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        if sha not in self.ci:
            return []
        return [{"name": "ci", "status": "completed", "conclusion": self.ci[sha], "html_url": "u"}]


def test_buggy_change_caught_reworked_approved_merged(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    db = api_tokens
    make_project(db, remote, test_command=None)
    db.execute("UPDATE projects SET require_ci = true")
    key = make_task(db, title="Update guide to v2")
    opener, github = FakePrOpener(), FakeReader()
    code_verdicts = iter([ReviewScenario.FAIL, ReviewScenario.PASS])  # round 1 bug, round 2 fixed

    def code_review() -> FakeReviewer:
        return FakeReviewer(scenario=next(code_verdicts))

    def state() -> TaskState:
        return get_task(db, key).state

    # Round 1: DEV-01 submits a change with a (planted) defect.
    assert make_worker(api_url, tmp_path, FakeRuntime(edits={"CLAUDE.md": "# guide v1?\n"}),
                       opener).run_once()  # fmt: skip
    assert state() == TaskState.IN_REVIEW
    # The independent code reviewer catches it; the task goes back at once.
    assert reviewer(
        api_url, tmp_path, lambda: FakeReviewer(scenario=next(code_verdicts))
    ).run_once()
    assert state() == TaskState.READY
    assert reviewer(api_url, tmp_path, lambda: FakeReviewer(), agent="SEC-01").run_once() is False

    # Round 2: DEV-02 reworks on the same branch and PR.
    assert make_worker(api_url, tmp_path, FakeRuntime(edits={"CLAUDE.md": "# guide v2\n"}),
                       opener, agent="DEV-02").run_once()  # fmt: skip
    assert state() == TaskState.IN_REVIEW
    assert reviewer(
        api_url, tmp_path, lambda: FakeReviewer(scenario=next(code_verdicts))
    ).run_once()
    assert reviewer(api_url, tmp_path, lambda: FakeReviewer(), agent="SEC-01").run_once()
    assert state() == TaskState.IN_REVIEW  # both reviews PASS, but CI has not reported yet

    # CI goes green for the round-2 commit; the sync tells Axiom; the aggregator approves.
    head = get_task(db, key).head_sha
    assert head is not None
    github.ci[head] = "success"
    assert sync_once(db, github) == {key: "ci success: approve"}  # type: ignore[arg-type]
    assert state() == TaskState.AWAITING_MERGE

    # The human merges on GitHub; the sync records it.
    github.merged = True
    assert sync_once(db, github) == {key: "merged"}  # type: ignore[arg-type]
    task = get_task(db, key)
    assert (task.state, task.review_rounds) == (TaskState.DONE, 1)

    # The story, as the event log tells it.
    moves = [(e["type"], e["to_state"], e["actor"]) for e in events_for(db, key) if e["to_state"]]
    assert moves == [
        ("TASK_CREATED", "PENDING", "human:tester"),
        ("TASK_READY", "READY", "system:dependencies"),
        ("TASK_ASSIGNED", "ASSIGNED", "agent:DEV-01"),
        ("AGENT_STARTED", "IN_PROGRESS", "agent:DEV-01"),
        ("TASK_SUBMITTED", "IN_REVIEW", "agent:DEV-01"),
        ("TASK_REOPENED", "READY", "system:aggregator"),  # code review FAIL
        ("TASK_ASSIGNED", "ASSIGNED", "agent:DEV-02"),
        ("AGENT_STARTED", "IN_PROGRESS", "agent:DEV-02"),
        ("TASK_SUBMITTED", "IN_REVIEW", "agent:DEV-02"),
        ("REVIEW_APPROVED", "AWAITING_MERGE", "system:aggregator"),  # reviews PASS + CI green
        ("TASK_DONE", "DONE", "human:github-bharathcmbox"),
    ]
    reviews = db.execute("SELECT round, role, status FROM reviews ORDER BY round, role").fetchall()
    assert [(r["round"], r["role"], r["status"]) for r in reviews] == [
        (1, "code_reviewer", "FAIL"), (1, "security_reviewer", "SUPERSEDED"),
        (2, "code_reviewer", "PASS"), (2, "security_reviewer", "PASS"),
    ]  # fmt: skip
    assert len(opener.prs) == 1 and len(opener.comments) == 1  # one PR, one "Round 2" comment
    assert task.budget_spent_usd > 0  # developer runs + reviews, all counted
