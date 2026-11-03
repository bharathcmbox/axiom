"""Phase 2 T7: the harness's own checks on an agent's work, before anything is published."""

import os
import subprocess
from pathlib import Path

import pytest

from axiom.runtimes.base import CommandResult
from axiom.worker.verify import check_changes, commit, run_tests
from axiom.workspaces.git import git
from axiom.workspaces.manager import Workspace, prepare_workspace

ALLOWED = ["agent/pyproject.toml", "CLAUDE.md", "agent/tests/"]
FAKE_ANTHROPIC = "sk-ant-api03-" + "A" * 40
FAKE_GITHUB = "github_pat_" + "B" * 40


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src"
    (src / "agent" / "tests").mkdir(parents=True)
    (src / "agent" / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (src / "agent" / "app.py").write_text("print('hi')\n")
    (src / "agent" / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (src / "CLAUDE.md").write_text("# guide\nold_secret = 'sk-ant-api03-" + "Z" * 40 + "'\n")
    run = lambda *a: subprocess.run(a, cwd=src, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "add", ".")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return prepare_workspace(
        tmp_path / "work", task_key="SOC-001", execution_id=1, repo_url=src.as_uri(),
        default_branch="main", token=None,
    )  # fmt: skip


def _problems(ws: Workspace) -> list[str]:
    return check_changes(ws.repo, ws.base_sha, ALLOWED).problems


def test_allowed_edit_passes(ws: Workspace) -> None:
    (ws.repo / "agent" / "pyproject.toml").write_text("[project]\nname = 'demo'\n# dev deps\n")
    (ws.repo / "agent" / "tests" / "test_new.py").write_text("def test_b():\n    pass\n")
    check = check_changes(ws.repo, ws.base_sha, ALLOWED)
    assert check.ok, check.problems
    assert check.files == ["agent/pyproject.toml", "agent/tests/test_new.py"]


def test_no_changes_is_a_problem(ws: Workspace) -> None:
    assert _problems(ws) == ["no changes were made"]


def test_edit_outside_allowed_paths(ws: Workspace) -> None:
    (ws.repo / "CLAUDE.md").write_text("# guide v2\n")
    (ws.repo / "agent" / "app.py").write_text("print('pwned')\n")
    assert _problems(ws) == ["agent/app.py: outside allowed_paths " + str(ALLOWED)]


def test_deletions_count(ws: Workspace) -> None:
    (ws.repo / "agent" / "app.py").unlink()
    assert any("agent/app.py" in p for p in _problems(ws))


def test_rename_out_of_a_forbidden_path_is_caught(ws: Workspace) -> None:
    git(["mv", "agent/app.py", "agent/tests/app.py"], cwd=ws.repo)
    assert any(p.startswith("agent/app.py:") for p in _problems(ws))


def test_secret_files_rejected_even_under_a_broad_allowance(ws: Workspace) -> None:
    (ws.repo / ".env").write_text("X=1\n")
    problems = check_changes(ws.repo, ws.base_sha, ["**"]).problems
    assert problems == [".env: secret-looking file may not be changed"]


def test_symlinks_rejected(ws: Workspace) -> None:
    os.symlink("/etc/passwd", ws.repo / "agent" / "tests" / "link")
    assert any("symlinks may not be added" in p for p in _problems(ws))


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f"KEY={FAKE_ANTHROPIC}", "Anthropic API key"),
        (f"token = '{FAKE_GITHUB}'", "GitHub token"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
        ("aws = AKIAABCDEFGHIJKLMNOP", "AWS access key"),
    ],
)
def test_credentials_in_added_lines(ws: Workspace, line: str, kind: str) -> None:
    (ws.repo / "agent" / "tests" / "conf.py").write_text(line + "\n")
    assert _problems(ws) == [f"agent/tests/conf.py: added line looks like a {kind}"]


def test_removing_a_secret_is_fine(ws: Workspace) -> None:
    (ws.repo / "CLAUDE.md").write_text("# guide\n")  # removes the old_secret line
    assert _problems(ws) == []


def test_tool_caches_are_ignored(ws: Workspace) -> None:
    (ws.repo / "agent" / ".pytest_cache").mkdir()
    (ws.repo / "agent" / ".pytest_cache" / "v").write_text("x")
    (ws.repo / "agent" / "__pycache__").mkdir()
    (ws.repo / "agent" / "__pycache__" / "app.cpython-314.pyc").write_bytes(b"\0")
    (ws.repo / "CLAUDE.md").write_text("# guide v2\n")
    check = check_changes(ws.repo, ws.base_sha, ALLOWED)
    assert check.ok and check.files == ["CLAUDE.md"]


def test_commit_records_the_agent_as_author(ws: Workspace) -> None:
    (ws.repo / "CLAUDE.md").write_text("# guide v2\n")
    check_changes(ws.repo, ws.base_sha, ALLOWED)
    sha = commit(ws.repo, "SOC-001: update guide", "Axiom agent DEV-01 <axiom@localhost>")
    assert git(["log", "-1", "--format=%an|%cn|%s", sha], cwd=ws.repo) == (
        "Axiom agent DEV-01|Axiom|SOC-001: update guide"
    )


class LocalRunner:
    """Test double for the sandbox: runs the command directly in the clean clone."""

    def exec(self, workdir: Path, outdir: Path, command: str, timeout: int) -> CommandResult:
        p = subprocess.run(command, shell=True, cwd=workdir, capture_output=True, text=True)
        return CommandResult(p.returncode, p.stdout + p.stderr)


def test_tests_run_on_a_clean_clone_of_the_commit(ws: Workspace, tmp_path: Path) -> None:
    (ws.repo / "CLAUDE.md").write_text("# guide v2\n")
    check_changes(ws.repo, ws.base_sha, ALLOWED)
    commit(ws.repo, "msg", "A <a@a>")
    (ws.repo / "UNCOMMITTED").write_text("agent junk")  # must not be visible to the test run
    cmd = "test ! -e UNCOMMITTED && grep -q 'guide v2' CLAUDE.md && echo all-good"
    result = run_tests(ws.repo, tmp_path / "verify", cmd, LocalRunner())
    assert result.passed and result.exit_code == 0 and "all-good" in result.output_tail
    failing = run_tests(ws.repo, tmp_path / "verify", "echo boom; exit 3", LocalRunner())
    assert (failing.passed, failing.exit_code) == (False, 3) and "boom" in failing.output_tail
