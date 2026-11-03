"""Phase 2 T5: workspaces are fresh clones on an axiom/* branch with no credentials in them."""

import subprocess
from pathlib import Path

import pytest

from axiom.workspaces.git import GitError, git
from axiom.workspaces.manager import Workspace, prepare_workspace, remove_workspace

TOKEN = "github_pat_TESTTOKEN_do_not_leak_1234567890"


def _sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare 'GitHub' repo whose main has tracked files; the source checkout also has an
    untracked .env that must never reach a workspace."""
    src = tmp_path / "src"
    src.mkdir()
    _sh("git", "init", "-q", "-b", "main", cwd=src)
    (src / "README.md").write_text("# demo\n")
    (src / ".gitignore").write_text(".env\n")
    (src / "agent").mkdir()
    (src / "agent" / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (src / ".env").write_text("SECRET=local-only\n")
    _sh("git", "add", ".", cwd=src)
    _sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    bare = tmp_path / "remote.git"
    _sh("git", "clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    return bare


def _prepare(tmp_path: Path, remote: Path, execution_id: int = 7) -> Workspace:
    return prepare_workspace(
        tmp_path / "work",
        task_key="AX-001",
        execution_id=execution_id,
        repo_url=remote.as_uri(),
        default_branch="main",
        token=TOKEN,
    )


def test_fresh_clone_on_an_axiom_branch(tmp_path: Path, remote: Path) -> None:
    ws = _prepare(tmp_path, remote)
    assert ws.branch == "axiom/AX-001-e7"
    repo = ws.repo
    assert git(["branch", "--show-current"], cwd=repo) == "axiom/AX-001-e7"
    assert ws.base_sha == _sh("git", "rev-parse", "main", cwd=remote)
    assert (repo / "agent" / "pyproject.toml").exists()
    assert ws.out.is_dir()


def test_untracked_local_files_never_reach_the_workspace(tmp_path: Path, remote: Path) -> None:
    ws = _prepare(tmp_path, remote)
    assert not (ws.repo / ".env").exists()


def test_no_token_anywhere_in_the_workspace(tmp_path: Path, remote: Path) -> None:
    ws = _prepare(tmp_path, remote)
    root = ws.root
    for path in root.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    config = git(["config", "--local", "--list"], cwd=ws.repo)
    assert TOKEN not in config and "credential" not in config
    assert git(["remote", "get-url", "origin"], cwd=ws.repo) == remote.as_uri()


def test_credential_helper_supplies_the_token_from_the_environment_only(tmp_path: Path) -> None:
    out = git(
        ["credential", "fill"],
        cwd=tmp_path,
        token=TOKEN,
        input_text="protocol=https\nhost=github.com\n\n",
    )
    assert f"password={TOKEN}" in out and "username=x-access-token" in out
    # Without a token there is no helper at all: git fails fast instead of prompting.
    with pytest.raises(GitError):
        git(["credential", "fill"], cwd=tmp_path, input_text="protocol=https\nhost=x\n\n")


def test_git_errors_never_echo_the_token(tmp_path: Path) -> None:
    with pytest.raises(GitError) as exc:
        git(["clone", f"https://{TOKEN}@127.0.0.1:9/nope.git", str(tmp_path / "x")], token=TOKEN)
    assert TOKEN not in str(exc.value)


def test_preparing_again_replaces_the_old_directory(tmp_path: Path, remote: Path) -> None:
    first = _prepare(tmp_path, remote)
    (first.repo / "junk.txt").write_text("left over")
    second = _prepare(tmp_path, remote)
    assert not (second.repo / "junk.txt").exists()


def test_remove_workspace(tmp_path: Path, remote: Path) -> None:
    ws = _prepare(tmp_path, remote)
    remove_workspace(ws)
    assert not ws.root.exists()


def test_rejects_unexpected_task_keys(tmp_path: Path, remote: Path) -> None:
    with pytest.raises(ValueError):
        prepare_workspace(
            tmp_path, task_key="../../etc", execution_id=1, repo_url=remote.as_uri(),
            default_branch="main", token=None,
        )  # fmt: skip


def test_global_git_config_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user's ~/.gitconfig credential helpers (e.g. gh's) must not be used by workers."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[credential "https://github.com"]\n\thelper = "!echo password=USER-LOGIN #"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    out = git(["config", "--list"], cwd=tmp_path)
    assert "USER-LOGIN" not in out
