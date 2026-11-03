"""Create and remove per-execution workspaces.

Layout under the workspaces root (one directory per execution):

    <root>/<TASK>-e<execution_id>/
        repo/   fresh clone on branch axiom/<TASK>-e<execution_id>   (mounted into the sandbox)
        out/    transcript, raw result, logs                           (mounted into the sandbox)

The clone comes from the remote (e.g. GitHub), never from a checkout on this machine, so only
tracked files are present: no local `.env`, no untracked secrets.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from axiom.workspaces.git import git

BRANCH_PREFIX = "axiom/"
COMMITTER_NAME = "Axiom"
COMMITTER_EMAIL = "axiom@localhost"
_SAFE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


@dataclass(frozen=True)
class Workspace:
    root: Path
    repo: Path
    out: Path
    branch: str
    base_sha: str


def branch_name(task_key: str, execution_id: int) -> str:
    return f"{BRANCH_PREFIX}{task_key}-e{execution_id}"


def prepare_workspace(
    workspaces_root: Path,
    *,
    task_key: str,
    execution_id: int,
    repo_url: str,
    default_branch: str,
    token: str | None,
) -> Workspace:
    if not _SAFE.match(task_key):
        raise ValueError(f"unexpected task key {task_key!r}")
    root = workspaces_root / f"{task_key}-e{execution_id}"
    if root.exists():
        shutil.rmtree(root)  # a previous attempt of the same execution id is never reused
    repo, out = root / "repo", root / "out"
    out.mkdir(parents=True)
    git(
        ["clone", "--quiet", "--single-branch", "--branch", default_branch, repo_url, str(repo)],
        token=token,
    )
    branch = branch_name(task_key, execution_id)
    git(["checkout", "--quiet", "-b", branch], cwd=repo)
    git(["config", "user.name", COMMITTER_NAME], cwd=repo)
    git(["config", "user.email", COMMITTER_EMAIL], cwd=repo)
    base_sha = git(["rev-parse", "HEAD"], cwd=repo)
    return Workspace(root=root, repo=repo, out=out, branch=branch, base_sha=base_sha)


def remove_workspace(workspace: Workspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)
