"""Phase 3 T4: the review workspace is the exact commit under review, with its diff vs main."""

import subprocess
from pathlib import Path

import pytest

from axiom.workspaces.git import git
from axiom.workspaces.manager import prepare_review_workspace, remove_review_workspace

TOKEN = "github_pat_TESTTOKEN_never_persisted_12345678901234"


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str, str]:
    """main has one commit; branch axiom/SOC-002-e5 has two more. Returns (bare, sha1, sha2)."""
    src = tmp_path / "src"
    src.mkdir()
    g = lambda *a: _run("git", "-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=src)  # noqa: E731
    g("init", "-q", "-b", "main")
    (src / "x.py").write_text("def f(a, b):\n    return a < b\n")
    g("add", ".")
    g("commit", "-qm", "init")
    g("checkout", "-qb", "axiom/SOC-002-e5")
    (src / "x.py").write_text("def f(a, b):\n    return a <= b\n")
    g("commit", "-qam", "fix compare")
    sha1 = g("rev-parse", "HEAD")
    (src / "test_x.py").write_text("def test_f():\n    assert f(1, 1)\n")
    g("add", ".")
    g("commit", "-qm", "add test")
    sha2 = g("rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    _run("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare, sha1, sha2


def test_reviews_the_exact_commit_with_its_diff(
    tmp_path: Path, remote: tuple[Path, str, str]
) -> None:
    bare, sha1, sha2 = remote
    ws = prepare_review_workspace(
        tmp_path / "work", task_key="SOC-002", review_id=9, repo_url=bare.as_uri(),
        branch="axiom/SOC-002-e5", head_sha=sha1, default_branch="main", token=TOKEN,
    )  # fmt: skip
    # The submitted commit, not the branch tip: reviewers see exactly what was submitted.
    assert ws.head_sha == sha1 and ws.head_sha != sha2
    assert "-    return a < b\n+    return a <= b" in ws.diff
    assert "test_x.py" not in ws.diff  # that commit came later
    assert not (ws.repo / "test_x.py").exists()
    assert ws.base_sha == _run("git", "rev-parse", "main", cwd=bare)


def test_branch_tip_when_no_sha(tmp_path: Path, remote: tuple[Path, str, str]) -> None:
    bare, _, sha2 = remote
    ws = prepare_review_workspace(
        tmp_path / "work", task_key="SOC-002", review_id=9, repo_url=bare.as_uri(),
        branch="axiom/SOC-002-e5", head_sha=None, default_branch="main", token=None,
    )  # fmt: skip
    assert ws.head_sha == sha2 and "test_x.py" in ws.diff


def test_no_token_in_the_review_workspace(tmp_path: Path, remote: tuple[Path, str, str]) -> None:
    bare, sha1, _ = remote
    ws = prepare_review_workspace(
        tmp_path / "work", task_key="SOC-002", review_id=9, repo_url=bare.as_uri(),
        branch="axiom/SOC-002-e5", head_sha=sha1, default_branch="main", token=TOKEN,
    )  # fmt: skip
    for path in ws.root.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert "credential" not in git(["config", "--local", "--list"], cwd=ws.repo)
    remove_review_workspace(ws)
    assert not ws.root.exists()
