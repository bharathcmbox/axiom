"""PreToolUse hooks: enforce allowed_paths and keep secrets unreadable, on every tool call.

The live spike showed a PreToolUse hook denies a call even when the tool is pre-approved in
`allowed_tools`, and the agent sees the reason. The harness re-checks the final diff anyway
(Bash can still write files; the diff check catches that).
"""

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from axiom.paths import is_allowed, is_secret

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read", "Glob", "Grep")

HookFn = Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def relative_to_workspace(path: str, workspace: Path) -> str | None:
    """Repository-relative form of a tool's path argument, or None if outside the workspace."""
    if not path:
        return None
    candidate = Path(path) if os.path.isabs(path) else workspace / path
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(workspace.resolve())
    except OSError, ValueError:
        return None
    return rel.as_posix() if str(rel) != "." else ""


def decide(
    tool_name: str, tool_input: dict[str, Any], workspace: Path, allowed: list[str]
) -> str | None:
    """None to allow, or the reason to deny. Pure, so it can be tested without the SDK."""
    if tool_name in EDIT_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        rel = relative_to_workspace(raw, workspace)
        if rel is None:
            return f"{raw} is outside the repository"
        if not is_allowed(rel, allowed):
            return f"{rel} is outside allowed_paths {allowed}"
        return None
    if tool_name in READ_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("path") or ""
        if not raw:
            return None  # Glob/Grep over the whole workspace
        rel = relative_to_workspace(raw, workspace)
        if rel is None:
            return f"{raw} is outside the repository"
        if rel and is_secret(rel):
            return f"{rel} looks like a secret and may not be read"
    return None


def make_guard(workspace: Path, allowed: list[str]) -> HookFn:
    async def guard(input_: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = decide(
            input_.get("tool_name", ""), input_.get("tool_input") or {}, workspace, allowed
        )
        return _deny(reason) if reason else {}

    return guard
