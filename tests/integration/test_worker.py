"""Phase 2 T9: the worker harness end to end.

Real: Axiom API (uvicorn on a random port, test DB), HTTP with agent tokens, git clone / commit /
push to a local bare remote, verification, test re-run on a clean clone.
Fake: the agent (FakeRuntime), the test runner's sandbox (LocalRunner) and the GitHub PR call.
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from axiom.api.app import create_app
from axiom.config import Settings
from axiom.domain.task_machine import TaskState
from axiom.orchestrator.leases import expire_leases
from axiom.orchestrator.tasks import create_task, get_task
from axiom.registry.projects import create_project
from axiom.runtimes.base import CommandResult
from axiom.runtimes.fake import FakeRuntime, Scenario
from axiom.state.db import Conn
from axiom.worker.api_client import AxiomClient
from axiom.worker.harness import Worker, WorkerConfig
from axiom.worker.publish import PullRequest
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import HUMAN, events_for


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("# guide\n")
    (src / "app.py").write_text("print('hi')\n")
    _run("git", "add", ".", cwd=src)
    _run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "remote.git"
    _run("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def api_url(api_tokens: Conn, db_url: str) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(database_url=db_url), start_reaper=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


class LocalRunner:
    def exec(self, workdir: Path, outdir: Path, command: str, timeout: int) -> CommandResult:
        p = subprocess.run(command, shell=True, cwd=workdir, capture_output=True, text=True)
        return CommandResult(p.returncode, p.stdout + p.stderr)


@dataclass
class FakePrOpener:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, repo_url: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        self.calls.append((repo_url, branch, base, title, body))
        return PullRequest(41, f"https://github.com/o/r/pull/{41 + len(self.calls)}", created=True)


def _project(db: Conn, remote: Path, test_command: str | None = "grep -q v2 CLAUDE.md") -> None:
    create_project(db, HUMAN, key="SOC", name="soc", repo_url=remote.as_uri(),
                   test_command=test_command)  # fmt: skip


def _task(db: Conn, **kw: Any) -> str:
    args: dict[str, Any] = {"project_key": "SOC", "title": "Update guide", "specification": "s",
                            "acceptance_criteria": ["guide says v2"],
                            "allowed_paths": ["CLAUDE.md"]} | kw  # fmt: skip
    return create_task(db, HUMAN, **args).key


def _worker(api_url: str, tmp_path: Path, runtime: FakeRuntime, opener: FakePrOpener,
            agent: str = "DEV-01", **cfg: Any) -> Worker:  # fmt: skip
    config = WorkerConfig(agent_key=agent, workspaces_root=tmp_path / "work", github_token=None,
                          heartbeat_seconds=0.2, **cfg)  # fmt: skip
    return Worker(config, AxiomClient(api_url, TEST_TOKENS[f"agent:{agent}"]), lambda: runtime,
                  LocalRunner(), opener)  # fmt: skip


def _remote_branches(remote: Path) -> list[str]:
    return _run(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=remote
    ).split()


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
