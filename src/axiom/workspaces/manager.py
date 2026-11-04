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

# Tool caches an agent's test runs create. Excluded locally (.git/info/exclude, never committed
# to the repo) so they are neither staged nor counted as changes outside allowed_paths.
LOCAL_EXCLUDES = (
    ".pytest_cache/", "__pycache__/", "*.pyc", ".mypy_cache/", ".ruff_cache/", ".venv/",
    "node_modules/", ".coverage", "htmlcov/",
)  # fmt: skip


@dataclass(frozen=True)
class Workspace:
    root: Path
    repo: Path
    out: Path
    branch: str
    base_sha: str  # where this round starts: changes are checked against this commit
    continued: bool = False  # True when building on an earlier round's branch


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
    existing_branch: str | None = None,
) -> Workspace:
    """A fresh clone. With `existing_branch` (rework after review), continue that branch so the
    new commits land on the same PR; if it no longer exists on the remote, start fresh."""
    if not _SAFE.match(task_key):
        raise ValueError(f"unexpected task key {task_key!r}")
    root = workspaces_root / f"{task_key}-e{execution_id}"
    if root.exists():
        shutil.rmtree(root)  # a previous attempt of the same execution id is never reused
    repo, out = root / "repo", root / "out"
    out.mkdir(parents=True)
    continued = existing_branch is not None and _remote_has_branch(repo_url, existing_branch, token)
    start = existing_branch if continued and existing_branch else default_branch
    git(
        ["clone", "--quiet", "--single-branch", "--branch", start, repo_url, str(repo)],
        token=token,
    )
    if continued and existing_branch:
        branch = existing_branch
    else:
        branch = branch_name(task_key, execution_id)
        git(["checkout", "--quiet", "-b", branch], cwd=repo)
    git(["config", "user.name", COMMITTER_NAME], cwd=repo)
    git(["config", "user.email", COMMITTER_EMAIL], cwd=repo)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as f:
        f.write("\n# added by Axiom: tool caches\n" + "\n".join(LOCAL_EXCLUDES) + "\n")
    base_sha = git(["rev-parse", "HEAD"], cwd=repo)
    return Workspace(root=root, repo=repo, out=out, branch=branch, base_sha=base_sha,
                     continued=continued)  # fmt: skip


def _remote_has_branch(repo_url: str, branch: str, token: str | None) -> bool:
    out = git(["ls-remote", "--heads", repo_url, f"refs/heads/{branch}"], token=token)
    return bool(out.strip())


def remove_workspace(workspace: Workspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)


@dataclass(frozen=True)
class ReviewWorkspace:
    root: Path
    repo: Path
    out: Path
    head_sha: str
    base_sha: str  # merge-base with the default branch
    diff: str


def prepare_review_workspace(
    workspaces_root: Path,
    *,
    task_key: str,
    review_id: int,
    repo_url: str,
    branch: str,
    head_sha: str | None,
    default_branch: str,
    token: str | None,
) -> ReviewWorkspace:
    """Clone the task's branch, check out the commit under review (detached), and compute the
    diff against the default branch. The reviewer sees exactly what will be merged."""
    if not _SAFE.match(task_key):
        raise ValueError(f"unexpected task key {task_key!r}")
    root = workspaces_root / f"{task_key}-r{review_id}"
    if root.exists():
        shutil.rmtree(root)
    repo, out = root / "repo", root / "out"
    out.mkdir(parents=True)
    git(["clone", "--quiet", "--branch", branch, repo_url, str(repo)], token=token)
    git(["fetch", "--quiet", "origin",
         f"refs/heads/{default_branch}:refs/remotes/origin/{default_branch}"],
        cwd=repo, token=token)  # fmt: skip
    if head_sha:
        git(["checkout", "--quiet", "--detach", head_sha], cwd=repo)
    head = git(["rev-parse", "HEAD"], cwd=repo)
    base = git(["merge-base", f"origin/{default_branch}", "HEAD"], cwd=repo)
    diff = git(["diff", "--no-color", "--find-renames", f"{base}..{head}"], cwd=repo)
    return ReviewWorkspace(root=root, repo=repo, out=out, head_sha=head, base_sha=base, diff=diff)


def remove_review_workspace(workspace: ReviewWorkspace) -> None:
    shutil.rmtree(workspace.root, ignore_errors=True)
