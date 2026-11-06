"""Phase 2 T9: the worker harness end to end.

Real: Axiom API (uvicorn on a random port, test DB), HTTP with agent tokens, git clone / commit /
push to a local bare remote, verification, test re-run on a clean clone.
Fake: the agent (FakeRuntime), the test runner's sandbox (LocalRunner) and the GitHub PR call.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskState
from axiom.orchestrator.leases import expire_leases
from axiom.orchestrator.tasks import get_task
from axiom.runtimes.base import RunOutcome, RunRequest
from axiom.runtimes.fake import FakeRuntime, Scenario
from axiom.state.db import Conn
from axiom.worker.api_client import AxiomClient
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import events_for
from tests.integration.workers_shared import (
    FakePrOpener,
    make_project,
    make_task,
    make_worker,
    remote_branches,
    run_git,
)

_run = run_git
_project = make_project
_task = make_task
_worker = make_worker
_remote_branches = remote_branches


GOOD_EDIT = {"CLAUDE.md": "# guide v2\n"}


def test_success_ends_in_review_with_a_pushed_branch_and_a_pr(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    key = _task(api_tokens)
    opener = FakePrOpener()
    worker = _worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener)

    assert worker.run_once() is True

    task = get_task(api_tokens, key)
    assert task.state == TaskState.IN_REVIEW
    assert task.branch == "axiom/SOC-001-e1" and task.pull_request_url == opener_url(opener)
    assert "axiom/SOC-001-e1" in _remote_branches(remote)
    assert _run("git", "show", "axiom/SOC-001-e1:CLAUDE.md", cwd=remote) == "# guide v2"
    assert _run("git", "log", "-1", "--format=%an", "axiom/SOC-001-e1", cwd=remote) == (
        "Axiom agent DEV-01"
    )
    [(repo_url, branch, base, title, body)] = opener.calls
    assert repo_url == remote.as_uri()
    assert (branch, base, title) == ("axiom/SOC-001-e1", "main", "SOC-001: Update guide")
    assert "✅ passed" in body  # verified test run
    execution = api_tokens.execute("SELECT * FROM executions").fetchone()
    assert execution is not None and execution["status"] == "COMPLETED"
    assert execution["cost_usd"] == Decimal("0.4200")
    assert execution["result"]["verified_tests"]["passed"] is True
    assert not (tmp_path / "work" / "SOC-001-e1").exists()  # workspace cleaned up


def opener_url(opener: FakePrOpener) -> str:
    return f"https://github.com/o/r/pull/{41 + len(opener.calls)}"


def test_nothing_to_claim(api_tokens: Conn, api_url: str, tmp_path: Path) -> None:
    assert _worker(api_url, tmp_path, FakeRuntime(), FakePrOpener()).run_once() is False


@pytest.mark.parametrize(
    ("scenario", "error_fragment"),
    [
        (Scenario.EDIT_OUTSIDE_ALLOWED, "NOT_ALLOWED.txt: outside allowed_paths"),
        (Scenario.LEAK_SECRET, "added line looks like a Anthropic API key"),
        (Scenario.FAIL, "agent crashed"),
        (Scenario.MALFORMED_RESULT, "invalid result"),
    ],
)
def test_bad_runs_fail_without_publishing(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path,
    scenario: Scenario, error_fragment: str,
) -> None:  # fmt: skip
    _project(api_tokens, remote)
    key = _task(api_tokens)
    opener = FakePrOpener()
    _worker(api_url, tmp_path, FakeRuntime(scenario=scenario, edits=GOOD_EDIT), opener).run_once()
    task = get_task(api_tokens, key)
    assert (task.state, task.attempt_count) == (TaskState.READY, 1)
    assert error_fragment in events_for(api_tokens, key)[-1]["payload"]["error"]
    assert _remote_branches(remote) == ["main"] and opener.calls == []


def test_failing_verified_tests_block_publishing(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote, test_command="echo 2 failed; exit 1")
    key = _task(api_tokens)
    opener = FakePrOpener()
    _worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener).run_once()
    payload = events_for(api_tokens, key)[-1]["payload"]
    assert payload["error"] == "verified tests failed (exit 1)"
    assert (
        payload["verified_tests"]["passed"] is False
        and "2 failed" in payload["verified_tests"]["output_tail"]
    )
    assert _remote_branches(remote) == ["main"] and opener.calls == []


def test_needs_input_asks_the_human(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    key = _task(api_tokens)
    _worker(
        api_url, tmp_path, FakeRuntime(scenario=Scenario.NEEDS_INPUT), FakePrOpener()
    ).run_once()
    assert get_task(api_tokens, key).state == TaskState.WAITING_FOR_HUMAN
    assert events_for(api_tokens, key)[-1]["payload"]["question"] == "Which DB?"


def test_budget_exceeded_fails_the_task(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    key = _task(api_tokens, budget_usd=Decimal("0.30"))
    runtime = FakeRuntime(scenario=Scenario.BUDGET_EXCEEDED)
    _worker(api_url, tmp_path, runtime, FakePrOpener()).run_once()
    task = get_task(api_tokens, key)
    assert task.state == TaskState.FAILED  # budget exhausted: no retry
    assert task.budget_spent_usd == Decimal("0.31")


def test_lost_lease_cancels_the_run_and_reports_nothing(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    key = _task(api_tokens)
    runtime = FakeRuntime(scenario=Scenario.HANG)
    worker = _worker(api_url, tmp_path, runtime, FakePrOpener())
    t = threading.Thread(target=worker.run_once)
    t.start()
    for _ in range(100):  # wait until the agent is running
        if get_task(api_tokens, key).state == TaskState.IN_PROGRESS:
            break
        time.sleep(0.05)
    api_tokens.execute("UPDATE executions SET lease_expires_at = now() - interval '1 second'")
    assert expire_leases(api_tokens) == [key]  # the reaper takes the task back
    t.join(timeout=10)
    assert not t.is_alive()  # heartbeat was rejected → run cancelled
    types = [e["type"] for e in events_for(api_tokens, key)]
    assert types[-1] == "EXECUTION_TIMED_OUT"  # the worker reported nothing afterwards
    assert get_task(api_tokens, key).state == TaskState.READY


USAGE_LINE = json.dumps(
    {
        "type": "AssistantMessage",
        "data": {
            "message_id": "m1",
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 100_000, "output_tokens": 10},
        },
    }
)


class SpendingHang(FakeRuntime):
    """Hangs until cancelled, like a real run: having spent money, with no final report."""

    def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
        outcome = super().run(request, on_event)
        (request.output_dir / "transcript.jsonl").write_text(USAGE_LINE + "\n")
        return outcome


def test_a_lost_lease_still_reports_what_the_run_cost(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    key = _task(api_tokens)
    worker = _worker(api_url, tmp_path, SpendingHang(scenario=Scenario.HANG), FakePrOpener())
    t = threading.Thread(target=worker.run_once, daemon=True)
    t.start()
    for _ in range(100):
        if get_task(api_tokens, key).state == TaskState.IN_PROGRESS:
            break
        time.sleep(0.05)
    api_tokens.execute("UPDATE executions SET lease_expires_at = now() - interval '1 second'")
    assert expire_leases(api_tokens) == [key]
    t.join(timeout=10)
    assert not t.is_alive()
    events = events_for(api_tokens, key)
    assert [e["type"] for e in events][-2:] == ["EXECUTION_TIMED_OUT", "EXECUTION_COST_RECORDED"]
    assert get_task(api_tokens, key).state == TaskState.READY  # money, not state
    assert get_task(api_tokens, key).budget_spent_usd == Decimal("0.4002")  # 100k x $4, 10 x $20


def test_a_restarted_worker_costs_the_run_it_lost_when_killed(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """Killing the worker leaves a workspace and a transcript. On restart it reads them."""
    _project(api_tokens, remote)
    key = _task(api_tokens)
    api = AxiomClient(api_url, TEST_TOKENS["agent:DEV-01"])
    assert api.claim(300) is not None
    execution_id = api.executions(key)[-1]["id"]
    out = tmp_path / "work" / f"{key}-e{execution_id}" / "out"
    out.mkdir(parents=True)
    (out / "transcript.jsonl").write_text(USAGE_LINE + "\n")
    api_tokens.execute("UPDATE executions SET lease_expires_at = now() - interval '1 second'")
    assert expire_leases(api_tokens) == [key]

    worker = _worker(api_url, tmp_path, FakeRuntime(), FakePrOpener())
    worker.stop()  # the sweep runs, and the loop then ends at once
    worker.run_forever()
    row = api.executions(key)[-1]
    assert row["id"] == execution_id
    assert Decimal(row["cost_usd"]) == Decimal("0.4002") and row["cost_estimated"] is True
    worker.run_forever()  # a second restart changes nothing
    assert get_task(api_tokens, key).budget_spent_usd == Decimal("0.4002")


def test_killed_worker_process_is_recovered_by_another_worker(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """SIGKILL a real worker process mid-run; its lease expires; DEV-02 completes the task."""
    _project(api_tokens, remote)
    key = _task(api_tokens)
    env = {
        **os.environ,
        "AXIOM_API_URL": api_url, "AXIOM_AGENT_KEY": "DEV-01",
        "AXIOM_AGENT_TOKEN": TEST_TOKENS["agent:DEV-01"],
        "AXIOM_WORKSPACES_DIR": str(tmp_path / "work-a"),
        "AXIOM_RUNTIME": "fake", "AXIOM_FAKE_SCENARIO": "hang",
        "AXIOM_WORKER_LEASE_SECONDS": "2", "AXIOM_HEARTBEAT_SECONDS": "0.5",
        "AXIOM_POLL_SECONDS": "0.2",
    }  # fmt: skip
    proc = subprocess.Popen([sys.executable, "-m", "axiom.worker"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(200):
            if get_task(api_tokens, key).state == TaskState.IN_PROGRESS:
                break
            time.sleep(0.05)
        assert get_task(api_tokens, key).state == TaskState.IN_PROGRESS
        time.sleep(1.0)  # heartbeats keep the 2s lease alive while the worker lives
        assert expire_leases(api_tokens) == []
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    time.sleep(2.5)  # no more heartbeats: the lease runs out
    assert expire_leases(api_tokens) == [key]
    opener = FakePrOpener()
    assert _worker(
        api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener, agent="DEV-02"
    ).run_once()
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW
    statuses = [r["status"] for r in api_tokens.execute(
        "SELECT status FROM executions ORDER BY id").fetchall()]  # fmt: skip
    assert statuses == ["TIMED_OUT", "COMPLETED"]
    assert "axiom/SOC-001-e2" in _remote_branches(remote)


def test_bad_repository_fails_fast_before_the_agent_runs(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    from axiom.worker.publish import parse_github_repo

    _project(api_tokens, remote)  # file:// URL: not a GitHub repository
    key = _task(api_tokens)
    runtime = FakeRuntime(edits=GOOD_EDIT)
    worker = _worker(api_url, tmp_path, runtime, FakePrOpener())
    worker.validate_repo = parse_github_repo
    worker.run_once()
    assert (
        "not a GitHub HTTPS repository URL" in events_for(api_tokens, key)[-1]["payload"]["error"]
    )
    assert _remote_branches(remote) == ["main"]  # nothing cloned, run or pushed
    assert not (tmp_path / "work").exists()


def test_agents_without_a_model_use_the_default(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote)
    _task(api_tokens)
    seen: list[str | None] = []

    class Recording(FakeRuntime):
        def run(self, request: Any, on_event: Any = None) -> Any:
            seen.append(request.model)
            return super().run(request, on_event)

    api_tokens.execute("UPDATE agents SET model = NULL WHERE key = 'DEV-01'")
    _worker(api_url, tmp_path, Recording(edits=GOOD_EDIT), FakePrOpener()).run_once()
    assert seen == ["claude-opus-5-5"]


# ---- Phase 3 T5: rework continues the same branch and PR --------------------------------


def _fail_review(db: Conn, key: str) -> None:
    from axiom.orchestrator.reviews import claim_review, submit_review

    finding = {"severity": "high", "file": "CLAUDE.md", "line": 1,
               "issue": "says v2, spec wants v3", "required_change": "write v3"}  # fmt: skip
    claimed = claim_review(db, Actor(ActorKind.AGENT, "REV-01"))
    assert claimed is not None
    verdict = {"verdict": "FAIL", "summary": "wrong version", "findings": [finding]}
    submit_review(db, Actor(ActorKind.AGENT, "REV-01"), claimed.review_id, verdict)


class RecordingRuntime(FakeRuntime):
    def __init__(self, edits: dict[str, str]) -> None:
        super().__init__(edits=edits)
        self.requests: list[Any] = []

    def run(self, request: Any, on_event: Any = None) -> Any:
        self.requests.append(request)
        return super().run(request, on_event)


def test_rework_continues_the_same_branch_and_pr_with_the_findings(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote, test_command=None)
    key = _task(api_tokens)
    opener = FakePrOpener()

    # round 1
    assert _worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener).run_once()
    first = get_task(api_tokens, key)
    assert (first.state, first.branch) == (TaskState.IN_REVIEW, "axiom/SOC-001-e1")

    # a reviewer fails it
    _fail_review(api_tokens, key)
    assert get_task(api_tokens, key).state == TaskState.READY

    # round 2: same branch, findings in the prompt, same PR, a round comment
    runtime = RecordingRuntime(edits={"CLAUDE.md": "# guide v3\n"})
    assert _worker(api_url, tmp_path, runtime, opener, agent="DEV-02").run_once()
    [request] = runtime.requests
    assert request.round == 2
    assert any("says v2, spec wants v3" in f for f in request.previous_feedback)
    second = get_task(api_tokens, key)
    assert (second.state, second.branch) == (TaskState.IN_REVIEW, "axiom/SOC-001-e1")
    assert second.pull_request_url == first.pull_request_url
    assert second.head_sha != first.head_sha
    log = _run("git", "log", "--format=%an|%s", "main..axiom/SOC-001-e1", cwd=remote).splitlines()
    assert len(log) == 2  # round 1 and round 2 commits, fast-forward on one branch
    assert log[0].startswith("Axiom agent DEV-02|") and log[1].startswith("Axiom agent DEV-01|")
    assert _run("git", "show", "axiom/SOC-001-e1:CLAUDE.md", cwd=remote) == "# guide v3"
    assert [c[0] for c in opener.comments] == [42]
    assert opener.comments[0][1].startswith("**Round 2**")
    rounds = api_tokens.execute(
        "SELECT round, count(*) AS n FROM reviews GROUP BY round ORDER BY round"
    ).fetchall()
    assert [(r["round"], r["n"]) for r in rounds] == [(1, 2), (2, 2)]


def test_rework_starts_fresh_if_the_branch_was_deleted(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _project(api_tokens, remote, test_command=None)
    key = _task(api_tokens)
    opener = FakePrOpener()
    _worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener).run_once()
    _fail_review(api_tokens, key)
    _run("git", "branch", "-D", "axiom/SOC-001-e1", cwd=remote)  # someone deleted it on GitHub
    _worker(api_url, tmp_path, FakeRuntime(edits=GOOD_EDIT), opener, agent="DEV-02").run_once()
    task = get_task(api_tokens, key)
    assert task.state == TaskState.IN_REVIEW and task.branch == "axiom/SOC-001-e2"  # new branch
