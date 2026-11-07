"""T10b: the reviewer comparison rebuilds what reviewers saw and never touches tasks or reviews."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.contracts import Finding, ReviewContract
from axiom.drills.review_compare import (
    Case,
    Spend,
    load_cases,
    prepare_snapshot,
    run_all,
    summarize,
)
from axiom.runtimes.base import Outcome
from axiom.runtimes.review import ReviewOutcome, ReviewRequest

SHA_A, SHA_B = "a" * 40, "b" * 40


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path: Path) -> dict[str, Any]:
    """main: A, later M. PR 7's commits B then C live only under refs/pull/7/head: its branch is
    gone, as after a merge."""
    src = tmp_path / "src"
    src.mkdir()
    _sh(src, "init", "-q", "-b", "main")
    _sh(src, "config", "user.email", "t@example.com")
    _sh(src, "config", "user.name", "t")
    (src / "app.py").write_text("x = 1\n")
    _sh(src, "add", ".")
    _sh(src, "commit", "-qm", "A")
    a = _sh(src, "rev-parse", "HEAD")
    _sh(src, "checkout", "-qb", "feature")
    (src / "app.py").write_text("x = 2\n")
    _sh(src, "commit", "-qam", "B")
    b = _sh(src, "rev-parse", "HEAD")
    (src / "app.py").write_text("x = 3\n")
    _sh(src, "commit", "-qam", "C")
    c = _sh(src, "rev-parse", "HEAD")
    _sh(src, "checkout", "-q", "main")
    (src / "other.py").write_text("later = True\n")
    _sh(src, "add", ".")
    _sh(src, "commit", "-qm", "M")
    _sh(src, "update-ref", "refs/pull/7/head", c)
    _sh(src, "branch", "-qD", "feature")
    bare = tmp_path / "remote.git"
    _sh(tmp_path, "clone", "-q", "--mirror", str(src), str(bare))
    return {"url": f"file://{bare}", "a": a, "b": b, "c": c}


def _case(**over: Any) -> Case:
    base: dict[str, Any] = {
        "task_key": "SOC-006",
        "pr": 7,
        "role": "code_reviewer",
        "repo_url": "https://github.com/o/r",
        "default_branch": "main",
        "head_sha": SHA_B,
        "base_sha": SHA_A,
        "title": "t",
        "specification": "s",
    }
    return Case(**{**base, **over})


def test_a_snapshot_is_the_reviewed_commit_and_its_diff_after_the_branch_is_gone(
    tmp_path: Path, pr_repo: dict[str, Any]
) -> None:
    """B was reviewed, then C was pushed and main moved on. The reviewer saw B against A."""
    case = _case(repo_url=pr_repo["url"], head_sha=pr_repo["b"], base_sha=pr_repo["a"])
    snap = prepare_snapshot(tmp_path / "work", "cmp-soc-006-code-sonnet", case, None)
    assert _sh(snap.repo, "rev-parse", "HEAD") == pr_repo["b"]
    assert "+x = 2" in snap.diff and "x = 3" not in snap.diff
    assert "other.py" not in snap.diff  # main's later work isn't the PR's
    assert (snap.repo.parent / "harness.git").is_dir()  # sealed before an agent sees it


def test_a_snapshot_is_never_reused(tmp_path: Path, pr_repo: dict[str, Any]) -> None:
    case = _case(repo_url=pr_repo["url"], head_sha=pr_repo["b"], base_sha=pr_repo["a"])
    prepare_snapshot(tmp_path, "cmp-x", case, None)
    with pytest.raises(FileExistsError):
        prepare_snapshot(tmp_path, "cmp-x", case, None)


@pytest.mark.parametrize(
    "over",
    [
        {"task_key": "../etc"},
        {"head_sha": "abc"},
        {"base_sha": "main"},
        {"role": "approver"},
    ],
)
def test_a_bad_case_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _case(**over)


def test_only_github_repositories_are_loaded(tmp_path: Path) -> None:
    f = tmp_path / "cases.json"
    f.write_text(json.dumps([{**_case().__dict__, "repo_url": "file:///etc"}]))
    with pytest.raises(ValueError, match="only GitHub"):
        load_cases(f)


class FakeReviewer:
    name = "fake"

    def __init__(self, summary: str = "fine", cost: str = "0.20") -> None:
        self.summary, self.cost = summary, cost
        self.requests: list[ReviewRequest] = []

    def run_review(self, request: ReviewRequest, on_event: Any = None) -> ReviewOutcome:
        self.requests.append(request)
        contract = ReviewContract(
            verdict="PASS",
            summary=self.summary,
            findings=[Finding(severity="low", issue="nit", required_change="tidy")],
        )
        return ReviewOutcome(Outcome.COMPLETED, contract=contract, cost_usd=Decimal(self.cost))

    def cancel(self) -> None: ...


def _run(
    tmp_path: Path, repo: dict[str, Any], reviewer: FakeReviewer, cap: str, models: list[str]
) -> tuple[list[dict[str, Any]], Path, Spend]:
    cases = [
        _case(
            repo_url=repo["url"],
            head_sha=repo["b"],
            base_sha=repo["a"],
            role=r,
            original={"verdict": "PASS"},
        )
        for r in ("code_reviewer", "security_reviewer")
    ]
    results, spend = tmp_path / "results.jsonl", Spend(cap=Decimal(cap))
    rows = run_all(
        cases,
        models,
        root=tmp_path / "work",
        results=results,
        runtime_factory=lambda: reviewer,
        token=None,
        budget=Decimal("1.50"),
        spend=spend,
        parallel=1,
    )
    return rows, results, spend


def test_every_case_runs_on_every_model_and_is_written_down(
    tmp_path: Path, pr_repo: dict[str, Any]
) -> None:
    reviewer = FakeReviewer()
    rows, results, _ = _run(
        tmp_path, pr_repo, reviewer, "12", ["claude-sonnet-5-5", "claude-fable-5-1"]
    )
    assert len(rows) == 4 and len(results.read_text().splitlines()) == 4
    assert {r.model for r in reviewer.requests} == {"claude-sonnet-5-5", "claude-fable-5-1"}
    assert all(r.review_id == 0 and "+x = 2" in r.diff for r in reviewer.requests)
    summary = summarize(rows, load_like(rows, pr_repo))
    assert summary["claude-sonnet-5-5"]["agree"] == 2
    assert summary["claude-sonnet-5-5"]["findings"] == {"low": 2}
    assert summary["claude-fable-5-1"]["cost_usd"] == "0.40"


def load_like(rows: list[dict[str, Any]], repo: dict[str, Any]) -> list[Case]:
    return [
        _case(repo_url=repo["url"], role=r, original={"verdict": "PASS"})
        for r in ("code_reviewer", "security_reviewer")
    ]


def test_the_drill_stops_starting_runs_at_its_cap(tmp_path: Path, pr_repo: dict[str, Any]) -> None:
    """Runs cost $1.20 each and may spend $1.50: a $3.20 cap starts two, then $2.40 is spent and
    a third could overrun it, so it and the fourth are skipped, not run."""
    reviewer = FakeReviewer(cost="1.20")
    models = ["claude-sonnet-5-5", "claude-fable-5-1"]
    rows, _, spend = _run(tmp_path, pr_repo, reviewer, "3.20", models)
    assert len(reviewer.requests) == len(rows) == 2
    assert spend.spent == Decimal("2.40") <= spend.cap


def test_a_credential_in_a_review_is_redacted_and_stops_the_drill(
    tmp_path: Path, pr_repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-ant-api03-" + "Q" * 40
    reviewer = FakeReviewer(summary=f"I found {secret} in the env")
    rows, results, spend = _run(tmp_path, pr_repo, reviewer, "12", ["claude-sonnet-5-5"])
    assert spend.stopped and rows[0]["credential_found"] is True
    assert secret not in results.read_text()
    assert len(reviewer.requests) == 1  # nothing else started


def test_bad_model_names_are_refused(tmp_path: Path, pr_repo: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _run(tmp_path, pr_repo, FakeReviewer(), "12", ["--evil"])
