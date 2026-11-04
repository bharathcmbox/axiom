"""Shared fixtures for worker / reviewer / full-loop tests: a real Axiom API on a random port,
a local bare git remote standing in for GitHub, and fakes for the PR opener and test runner."""

import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from axiom.api.app import create_app
from axiom.config import Settings
from axiom.orchestrator.tasks import create_task
from axiom.registry.projects import create_project
from axiom.runtimes.base import CommandResult
from axiom.runtimes.fake import FakeRuntime
from axiom.state.db import Conn
from axiom.worker.api_client import AxiomClient
from axiom.worker.harness import Worker, WorkerConfig
from axiom.worker.publish import PullRequest
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import HUMAN


def run_git(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    run_git("git", "init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("# guide\n")
    (src / "app.py").write_text("print('hi')\n")
    run_git("git", "add", ".", cwd=src)
    run_git("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "remote.git"
    run_git("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
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
    """Like GitHub: one open PR per branch; opening again returns the existing one."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    prs: dict[str, PullRequest] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)

    def __call__(self, repo_url: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        self.calls.append((repo_url, branch, base, title, body))
        if branch in self.prs:
            return PullRequest(self.prs[branch].number, self.prs[branch].url, created=False)
        number = 41 + len(self.prs) + 1
        self.prs[branch] = PullRequest(number, f"https://github.com/o/r/pull/{number}", True)
        return self.prs[branch]

    def comment(self, repo_url: str, number: int, body: str) -> None:
        self.comments.append((number, body))


def make_project(db: Conn, remote: Path, test_command: str | None = "grep -q v2 CLAUDE.md") -> None:
    create_project(db, HUMAN, key="SOC", name="soc", repo_url=remote.as_uri(),
                   test_command=test_command)  # fmt: skip


def make_task(db: Conn, **kw: Any) -> str:
    args: dict[str, Any] = {"project_key": "SOC", "title": "Update guide", "specification": "s",
                            "acceptance_criteria": ["guide says v2"],
                            "allowed_paths": ["CLAUDE.md"]} | kw  # fmt: skip
    return create_task(db, HUMAN, **args).key


def make_worker(api_url: str, tmp_path: Path, runtime: FakeRuntime, opener: FakePrOpener,
            agent: str = "DEV-01", **cfg: Any) -> Worker:  # fmt: skip
    config = WorkerConfig(agent_key=agent, workspaces_root=tmp_path / "work", github_token=None,
                          heartbeat_seconds=0.2, **cfg)  # fmt: skip
    return Worker(config, AxiomClient(api_url, TEST_TOKENS[f"agent:{agent}"]), lambda: runtime,
                  LocalRunner(), opener, comment_pr=opener.comment)  # fmt: skip


def remote_branches(remote: Path) -> list[str]:
    return run_git(
        "git", "for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=remote
    ).split()
