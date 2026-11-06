"""Verify an agent's work before anything leaves the machine (ADR-008, SPEC "trust CI").

1. Stage everything the agent changed and list the paths (adds, edits, deletes, renames).
2. Reject: no changes, paths outside allowed_paths, secret-looking files, symlinks, and
   added lines that look like credentials.
3. Commit locally, then run the project's test command in a *fresh* sandbox on a clean clone
   of that commit. The verified result is what Axiom records, not the agent's claim.
"""

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from axiom.paths import is_allowed, is_secret
from axiom.runtimes.base import CommandRunner
from axiom.workspaces.git import clone_source, git

# Patterns for credentials in added lines. Specific formats only, to keep false positives rare.
SECRET_RES = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "GitHub token": re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    ),
    "Axiom API token": re.compile(r"\baxm_[A-Za-z0-9_\-]{30,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}  # fmt: skip


@dataclass(frozen=True)
class Change:
    status: str  # A, M, D, R, T ...
    path: str
    old_path: str | None = None
    mode: str | None = None


@dataclass
class ChangeCheck:
    changes: list[Change]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def files(self) -> list[str]:
        return sorted({c.path for c in self.changes})


def staged_changes(repo: Path, base_sha: str) -> list[Change]:
    git(["add", "--all"], cwd=repo)
    out = git(["diff", "--cached", "--name-status", "-M", "-z", base_sha], cwd=repo)
    tokens = [t for t in out.split("\0") if t]
    modes = _new_modes(repo, base_sha)
    changes, i = [], 0
    while i < len(tokens):
        status = tokens[i]
        if status.startswith(("R", "C")):
            old, new = tokens[i + 1], tokens[i + 2]
            changes.append(Change(status[0], new, old_path=old, mode=modes.get(new)))
            i += 3
        else:
            path = tokens[i + 1]
            changes.append(Change(status[0], path, mode=modes.get(path)))
            i += 2
    return changes


def _new_modes(repo: Path, base_sha: str) -> dict[str, str]:
    raw = git(["diff", "--cached", "--raw", "-z", "--no-renames", base_sha], cwd=repo)
    tokens = [t for t in raw.split("\0") if t]
    modes = {}
    for meta, path in zip(tokens[::2], tokens[1::2], strict=False):
        parts = meta.split()  # ":oldmode newmode oldsha newsha status"
        if len(parts) >= 2:
            modes[path] = parts[1]
    return modes


def added_lines(repo: Path, base_sha: str) -> list[tuple[str, str]]:
    diff = git(["diff", "--cached", "--unified=0", "--no-color", base_sha], cwd=repo)
    current, out = "", []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current, line[1:]))
    return out


def credentials_in(text: str) -> list[str]:
    """The kinds of credential that appear in `text`. Never the values: this ends up in logs."""
    return [name for name, regex in SECRET_RES.items() if regex.search(text)]


def nested_git(repo: Path) -> list[str]:
    """Any `.git` below the top of the checkout. `git status` runs a nested repository's own
    config, so these are refused before the harness's git looks at the tree."""
    found = []
    for folder, dirs, files in os.walk(repo):
        for name in dirs + files:
            if name.lower() == ".git" and Path(folder) != repo:
                found.append((Path(folder) / name).relative_to(repo).as_posix())
    return sorted(found)


def check_changes(repo: Path, base_sha: str, allowed_paths: list[str]) -> ChangeCheck:
    nested = nested_git(repo)
    if nested:
        return ChangeCheck(
            [], [f"{path}: nested git repositories may not be added" for path in nested]
        )
    changes = staged_changes(repo, base_sha)
    check = ChangeCheck(changes)
    if not changes:
        check.problems.append("no changes were made")
    for c in changes:
        for path in filter(None, (c.path, c.old_path)):
            if is_secret(path):
                check.problems.append(f"{path}: secret-looking file may not be changed")
            elif not is_allowed(path, allowed_paths):
                check.problems.append(f"{path}: outside allowed_paths {allowed_paths}")
        if c.mode == "120000":
            check.problems.append(f"{c.path}: symlinks may not be added")
    for path, line in added_lines(repo, base_sha):
        for name, regex in SECRET_RES.items():
            if regex.search(line):
                check.problems.append(f"{path}: added line looks like a {name}")
    check.problems = list(dict.fromkeys(check.problems))
    return check


def commit(repo: Path, message: str, author: str) -> str:
    git(["commit", "--quiet", "--no-verify", "--author", author, "-m", message], cwd=repo)
    return git(["rev-parse", "HEAD"], cwd=repo)


# ---- verified test run ---------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedTests:
    command: str
    exit_code: int
    passed: bool
    output_tail: str


def run_tests(
    repo: Path, scratch: Path, command: str, runner: CommandRunner, timeout: int = 900
) -> VerifiedTests:
    """Clone the committed state into a clean directory and run `command` there."""
    clean, out = scratch / "repo", scratch / "out"
    if scratch.exists():
        shutil.rmtree(scratch)
    out.mkdir(parents=True)
    git(["clone", "--quiet", "--no-hardlinks", str(clone_source(repo)), str(clean)])
    result = runner.exec(clean, out, command, timeout)
    return VerifiedTests(
        command=command,
        exit_code=result.exit_code,
        passed=result.exit_code == 0,
        output_tail=result.output[-4000:],
    )
