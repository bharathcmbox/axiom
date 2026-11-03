"""Path rules shared by the sandbox hooks and the harness's diff check.

`allowed_paths` entries are repository-relative and may be:
- an exact file: `agent/pyproject.toml`
- a directory prefix ending in `/`: `agent/tests/`
- a glob: `*` (within one path segment), `?`, and `**` (any number of segments): `agent/**/*.py`
"""

import re
from functools import lru_cache
from pathlib import PurePosixPath

# Files an agent must not read or write even if a task allows the directory they are in.
SECRET_PATTERNS = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.secrets",
    "**/.secrets/**",
    "**/.git-credentials",
    "**/.netrc",
    ".git/**",
)
SECRET_EXCEPTIONS = ("**/.env.example", "**/.env.sample")


def normalize(path: str) -> str | None:
    """A clean repository-relative POSIX path, or None if it is absolute or escapes the repo."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute():
        return None
    parts: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) if parts else None


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return _compile(pattern).match(path) is not None


def is_allowed(path: str, allowed_paths: list[str]) -> bool:
    """True if a repository-relative path may be changed under `allowed_paths`."""
    rel = normalize(path)
    if rel is None or is_secret(rel):
        return False
    return any(matches(rel, p) for p in allowed_paths)


def is_secret(path: str) -> bool:
    rel = normalize(path)
    if rel is None:
        return True
    if any(matches(rel, p) for p in SECRET_EXCEPTIONS):
        return False
    return any(matches(rel, p) for p in SECRET_PATTERNS)
