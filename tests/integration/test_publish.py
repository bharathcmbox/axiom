"""Phase 2 T8: push only axiom/* branches; open or reuse a PR; nothing that could merge."""

import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from axiom.worker.publish import (
    GitHubClient,
    PublishError,
    assert_safe_branch,
    open_pull_request,
    parse_github_repo,
    pr_body,
    push_branch,
)
from axiom.workspaces.git import GitError, git
from axiom.workspaces.manager import Workspace, prepare_workspace


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=src)
    (src / "CLAUDE.md").write_text("# guide\n")
    _run("git", "add", ".", cwd=src)
    _run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "remote.git"
    _run("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def ws(tmp_path: Path, remote: Path) -> Workspace:
    w = prepare_workspace(
        tmp_path / "work", task_key="SOC-001", execution_id=3, repo_url=remote.as_uri(),
        default_branch="main", token=None,
    )  # fmt: skip
    (w.repo / "CLAUDE.md").write_text("# guide v2\n")
    git(["commit", "-qam", "change"], cwd=w.repo)
    return w


# ---- push ---------------------------------------------------------------------------------


def test_push_creates_only_the_axiom_branch(ws: Workspace, remote: Path) -> None:
    main_before = _run("git", "rev-parse", "main", cwd=remote)
    push_branch(ws.repo, ws.branch, token=None)
    assert _run("git", "rev-parse", ws.branch, cwd=remote) == git(
        ["rev-parse", "HEAD"], cwd=ws.repo
    )
    assert _run("git", "rev-parse", "main", cwd=remote) == main_before


@pytest.mark.parametrize(
    "branch",
    ["main", "refs/heads/main", "axiom/../main", "axiom/SOC-001", "axiom/soc-1-e1", "feature/x",
     "axiom/SOC-001-e1:main", "axiom/SOC-001-e1 main"],
)  # fmt: skip
def test_unsafe_refs_are_refused(ws: Workspace, remote: Path, branch: str) -> None:
    with pytest.raises(PublishError):
        push_branch(ws.repo, branch, token=None)
    assert _run("git", "branch", "--list", cwd=remote) == "* main"


def test_push_never_forces(ws: Workspace, remote: Path) -> None:
    push_branch(ws.repo, ws.branch, token=None)
    git(["reset", "--hard", "HEAD~1"], cwd=ws.repo)  # rewrite history locally
    (ws.repo / "CLAUDE.md").write_text("# different\n")
    git(["commit", "-qam", "rewritten"], cwd=ws.repo)
    with pytest.raises(GitError):  # non-fast-forward is rejected, not forced
        push_branch(ws.repo, ws.branch, token=None)


# ---- GitHub API -----------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.open_prs: list[dict[str, Any]] = []
        self.fail_post = False


@pytest.fixture
def github() -> Iterator[tuple[FakeGitHub, str]]:
    state = FakeGitHub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _reply(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self, body: Any = None) -> None:
            state.requests.append({"method": self.command, "path": self.path, "body": body,
                                   "auth": self.headers.get("Authorization")})  # fmt: skip

        def do_GET(self) -> None:
            self._record()
            self._reply(200, state.open_prs)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._record(body)
            if state.fail_post:
                self._reply(422, {"message": "Validation Failed"})
                return
            pr = {"number": 12, "html_url": "https://github.com/o/r/pull/12"}
            state.open_prs.append(pr)
            self._reply(201, pr)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


REPO = "https://github.com/bharathcmbox/tiny-agenticsoc.git"


def test_opens_a_pr_then_reuses_it(github: tuple[FakeGitHub, str]) -> None:
    state, api = github
    client = GitHubClient(token="github_pat_TEST", api=api)
    kwargs = {"branch": "axiom/SOC-001-e3", "base": "main", "title": "SOC-001: x", "body": "b"}
    first = open_pull_request(client, REPO, **kwargs)
    second = open_pull_request(client, REPO, **kwargs)
    assert (first.number, first.created, second.created) == (12, True, False)
    methods = [(r["method"], r["path"].split("?")[0]) for r in state.requests]
    assert methods == [
        ("GET", "/repos/bharathcmbox/tiny-agenticsoc/pulls"),
        ("POST", "/repos/bharathcmbox/tiny-agenticsoc/pulls"),
        ("GET", "/repos/bharathcmbox/tiny-agenticsoc/pulls"),
    ]
    post = state.requests[1]
    assert post["body"] == {"title": "SOC-001: x", "head": "axiom/SOC-001-e3", "base": "main",
                            "body": "b", "draft": False}  # fmt: skip
    assert post["auth"] == "Bearer github_pat_TEST"
    assert "head=bharathcmbox:axiom/SOC-001-e3" in state.requests[0]["path"]


def test_github_errors_surface(github: tuple[FakeGitHub, str]) -> None:
    state, api = github
    state.fail_post = True
    with pytest.raises(PublishError, match="422"):
        open_pull_request(GitHubClient(token="t", api=api), REPO, branch="axiom/SOC-001-e3",
                          base="main", title="t", body="b")  # fmt: skip


def test_client_has_no_way_to_merge() -> None:
    methods = {
        n for n in dir(GitHubClient) if not n.startswith("_") and callable(getattr(GitHubClient, n))
    }
    assert methods == {"find_open_pr", "create_pr"}


def test_refuses_unsafe_branch_before_any_api_call(github: tuple[FakeGitHub, str]) -> None:
    state, api = github
    with pytest.raises(PublishError):
        open_pull_request(GitHubClient(token="t", api=api), REPO, branch="main", base="main",
                          title="t", body="b")  # fmt: skip
    assert state.requests == []


@pytest.mark.parametrize(
    ("url", "parsed"),
    [
        ("https://github.com/o/r.git", ("o", "r")),
        ("https://github.com/o/r", ("o", "r")),
        (
            "https://github.com/bharathcmbox/tiny-agenticsoc.git",
            ("bharathcmbox", "tiny-agenticsoc"),
        ),
    ],
)
def test_parse_github_repo(url: str, parsed: tuple[str, str]) -> None:
    assert parse_github_repo(url) == parsed


@pytest.mark.parametrize("url", ["git@github.com:o/r.git", "https://evil.example/o/r", "file:///x"])
def test_parse_github_repo_rejects_others(url: str) -> None:
    with pytest.raises(PublishError):
        parse_github_repo(url)


def test_safe_branch_pattern() -> None:
    assert_safe_branch("axiom/SOC-001-e3")


def test_pr_body() -> None:
    body = pr_body(
        task_key="SOC-001", title="Add pytest", agent="DEV-01", execution_id=3,
        result={"summary": "Added pytest", "criteria": [
            {"criterion": "tests run", "met": True, "evidence": "8 passed"}],
            "known_limitations": ["none really"]},
        verified_tests={"command": "uv run pytest", "passed": True, "exit_code": 0},
        cost_usd="0.84",
    )  # fmt: skip
    assert "| tests run | ✅ | 8 passed |" in body
    assert "`uv run pytest`: ✅ passed (exit 0)" in body
    assert "Axiom never merges" in body and "$0.84" in body and "- none really" in body
