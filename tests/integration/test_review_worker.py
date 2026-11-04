"""Phase 3 T7: the reviewer worker end to end (real API over HTTP, real git, fake reviewer)."""

from pathlib import Path
from typing import Any

import pytest

from axiom.domain.task_machine import TaskState
from axiom.orchestrator.tasks import get_task
from axiom.runtimes.fake import FakeRuntime
from axiom.runtimes.fake_review import FakeReviewer, ReviewScenario
from axiom.state.db import Conn
from axiom.worker.api_client import ApiError, AxiomClient
from axiom.worker.review_harness import ReviewWorker, ReviewWorkerConfig
from tests.conftest import TEST_TOKENS
from tests.integration.workers_shared import FakePrOpener, make_project, make_task, make_worker

GOOD_EDIT = {"CLAUDE.md": "# guide v2\n"}


def reviewer(api_url: str, tmp_path: Path, factory: Any, agent: str = "REV-01") -> ReviewWorker:
    config = ReviewWorkerConfig(agent_key=agent, workspaces_root=tmp_path / f"rw-{agent}",
                                github_token=None, heartbeat_seconds=0.2)  # fmt: skip
    return ReviewWorker(config, AxiomClient(api_url, TEST_TOKENS[f"agent:{agent}"]), factory)


def _submitted(db: Conn, api_url: str, remote: Path, tmp_path: Path) -> str:
    make_project(db, remote, test_command=None)
    key = make_task(db)
    assert make_worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), FakePrOpener()).run_once()
    assert get_task(db, key).state == TaskState.IN_REVIEW
    return key


def _reviews(db: Conn) -> list[dict[str, Any]]:
    return db.execute("SELECT role, round, status, attempt_count, error, verdict FROM reviews "
                      "ORDER BY round, role").fetchall()  # fmt: skip


def test_two_passing_reviewers_approve(api_tokens: Conn, api_url: str, remote: Path,
                                       tmp_path: Path) -> None:  # fmt: skip
    key = _submitted(api_tokens, api_url, remote, tmp_path)
    passing = lambda: FakeReviewer(scenario=ReviewScenario.PASS)  # noqa: E731
    assert reviewer(api_url, tmp_path, passing).run_once()
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW
    assert reviewer(api_url, tmp_path, passing, agent="SEC-01").run_once()
    assert get_task(api_tokens, key).state == TaskState.AWAITING_MERGE
    assert [r["status"] for r in _reviews(api_tokens)] == ["PASS", "PASS"]
    assert not any((tmp_path / "rw-REV-01").glob("*"))  # review workspace cleaned up


def test_a_failing_reviewer_sends_work_back(api_tokens: Conn, api_url: str, remote: Path,
                                            tmp_path: Path) -> None:  # fmt: skip
    key = _submitted(api_tokens, api_url, remote, tmp_path)
    assert reviewer(
        api_url, tmp_path, lambda: FakeReviewer(scenario=ReviewScenario.FAIL)
    ).run_once()
    assert get_task(api_tokens, key).state == TaskState.READY
    # SEC-01's review was superseded: nothing left for it to do.
    passing = lambda: FakeReviewer(scenario=ReviewScenario.PASS)  # noqa: E731
    assert reviewer(api_url, tmp_path, passing, agent="SEC-01").run_once() is False


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (ReviewScenario.TRY_TO_EDIT, "reviewer modified the workspace"),
        (ReviewScenario.CRASH, "reviewer crashed"),
        (ReviewScenario.MALFORMED, "invalid review contract"),
    ],
)
def test_bad_review_runs_are_retried(api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path,
                                     scenario: ReviewScenario, error: str) -> None:  # fmt: skip
    key = _submitted(api_tokens, api_url, remote, tmp_path)
    reviewer(api_url, tmp_path, lambda: FakeReviewer(scenario=scenario)).run_once()
    code = next(r for r in _reviews(api_tokens) if r["role"] == "code_reviewer")
    assert (code["status"], code["attempt_count"]) == ("PENDING", 1)
    assert error in code["error"]
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW


def test_round_two_reviewers_get_the_previous_findings(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    key = _submitted(api_tokens, api_url, remote, tmp_path)
    reviewer(api_url, tmp_path, lambda: FakeReviewer(scenario=ReviewScenario.FAIL)).run_once()
    make_worker(api_url, tmp_path, FakeRuntime(edits={"CLAUDE.md": "# v3\n"}), FakePrOpener(),
                agent="DEV-02").run_once()  # fmt: skip
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW
    seen: list[Any] = []

    class Recording(FakeReviewer):
        def run_review(self, request: Any, on_event: Any = None) -> Any:
            seen.append(request)
            return super().run_review(request, on_event)

    reviewer(api_url, tmp_path, lambda: Recording(scenario=ReviewScenario.PASS)).run_once()
    [request] = seen
    assert request.model == "claude-fable-5-1"
    assert any("code_reviewer: planted defect" in f for f in request.previous_findings)
    assert "# v3" in request.diff


def test_developers_cannot_act_as_reviewers(api_tokens: Conn, api_url: str, remote: Path,
                                            tmp_path: Path) -> None:  # fmt: skip
    _submitted(api_tokens, api_url, remote, tmp_path)
    with pytest.raises(ApiError) as exc:
        reviewer(api_url, tmp_path, lambda: FakeReviewer(), agent="DEV-01").run_once()
    assert exc.value.status == 403


def test_nothing_to_review(api_tokens: Conn, api_url: str, tmp_path: Path) -> None:
    assert reviewer(api_url, tmp_path, lambda: FakeReviewer()).run_once() is False
