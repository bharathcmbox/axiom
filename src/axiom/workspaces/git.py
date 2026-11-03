"""Run git without ever writing a token to disk or putting it on a command line.

The token travels only in the environment of the git subprocess (`AXIOM_GIT_TOKEN`). An inline
credential helper reads it when git asks for a password. Nothing is persisted: no token in the
remote URL, in `.git/config`, or in a credential store.
"""

import os
import subprocess
from pathlib import Path

_HELPER = '!f() { echo username=x-access-token; echo "password=$AXIOM_GIT_TOKEN"; }; f'


class GitError(Exception):
    pass


def git(
    args: list[str],
    *,
    cwd: Path | None = None,
    token: str | None = None,
    timeout: int = 300,
    input_text: str | None = None,
) -> str:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_TERMINAL_PROMPT": "0",  # never block waiting for a password
        "GIT_CONFIG_NOSYSTEM": "1",
        # Ignore ~/.gitconfig too: it may hold URL-specific credential helpers (e.g. `gh`'s)
        # that `-c credential.helper=` does not clear, letting git use someone else's login.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    cmd = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null"]
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
