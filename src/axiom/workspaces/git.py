"""Run git without ever writing a token to disk or putting it on a command line, and without
trusting anything an agent could have written.

The token travels only in the environment of the git subprocess (`AXIOM_GIT_TOKEN`). An inline
credential helper reads it when git asks for a password. Nothing is persisted: no token in the
remote URL, in `.git/config`, or in a credential store.

The sandbox can write to a checkout, including its `.git`. Git executes what that config says
(`core.fsmonitor`, clean filters) and sends credentials where it says (`url.*.insteadOf`), and hooks
are only one of those ways. So once a checkout is sealed, the harness's git uses a private copy of
`.git` kept outside the sandbox's mounts, and never reads the agent's (ADR-014). A git subprocess
gets only the environment it needs: a compromised git can't read what it was never given.
"""

import os
import shutil
import subprocess
from pathlib import Path

_HELPER = '!f() { echo username=x-access-token; echo "password=$AXIOM_GIT_TOKEN"; }; f'
HARNESS_GIT_DIR = "harness.git"  # next to repo/ and out/, which are all the sandbox gets mounted
_PASSED_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")


class GitError(Exception):
    pass


def seal(repo: Path) -> None:
    """Call before an agent can touch `repo`: from here on the harness uses its own `.git`."""
    shutil.copytree(repo / ".git", repo.parent / HARNESS_GIT_DIR)


def harness_git_dir(repo: Path) -> Path | None:
    candidate = repo.parent / HARNESS_GIT_DIR
    return candidate if repo.name == "repo" and candidate.is_dir() else None


def clone_source(repo: Path) -> Path:
    """Where to clone a checkout from: never the agent's `.git`."""
    return harness_git_dir(repo) or repo


def git(
    args: list[str],
    *,
    cwd: Path | None = None,
    token: str | None = None,
    timeout: int = 300,
    input_text: str | None = None,
) -> str:
    env = {
        **{k: os.environ[k] for k in _PASSED_ENV if k in os.environ},
        "GIT_TERMINAL_PROMPT": "0",  # never block waiting for a password
        "GIT_CONFIG_NOSYSTEM": "1",
        # Ignore ~/.gitconfig too: it may hold URL-specific credential helpers (e.g. `gh`'s)
        # that `-c credential.helper=` does not clear, letting git use someone else's login.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    cmd = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null"]
    cmd += ["-c", "core.fsmonitor=false"]
    sealed = harness_git_dir(cwd) if cwd is not None else None
    if sealed is not None and cwd is not None:
        cmd += ["--git-dir", str(sealed), "--work-tree", str(cwd)]
    if token is not None:
        env["AXIOM_GIT_TOKEN"] = token
        cmd += ["-c", f"credential.helper={_HELPER}"]
    proc = subprocess.run(
        cmd + args,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        if token:
            message = message.replace(token, "***")
        raise GitError(f"git {args[0]} failed: {message}")
    return proc.stdout.strip()
