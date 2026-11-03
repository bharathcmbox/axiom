"""Phase 2 T1 live spike: verify three Claude Agent SDK behaviours Axiom relies on.

1. A PreToolUse hook denies an Edit/Write outside the allowed paths, even when the tool is in
   allowed_tools, and the agent is told why.
2. ResultMessage reports total_cost_usd and usage.
3. A tiny max_budget_usd ends the run with subtype "error_max_budget_usd".

Runs in a throwaway temp directory. Requires ANTHROPIC_API_KEY in the environment.
Costs a few cents. Usage (from a venv that has claude-agent-sdk installed):
    python scripts/spike_agent_sdk.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

MODEL = "claude-opus-5-5"


def make_hook(root: Path, allowed: list[str]) -> Any:
    async def guard(input_: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        path = input_.get("tool_input", {}).get("file_path", "")
        rel = os.path.relpath(Path(path).resolve(), root.resolve())
        if rel in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{rel} is outside allowed_paths {allowed}",
            }
        }

    return guard


async def run(prompt: str, root: Path, budget: float) -> ResultMessage | None:
    options = ClaudeAgentOptions(
        model=MODEL,
        effort="low",
        cwd=str(root),
        tools=["Read", "Edit", "Write"],
        allowed_tools=["Read", "Edit", "Write"],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=8,
        max_budget_usd=budget,
        hooks={
            "PreToolUse": [HookMatcher(matcher="Edit|Write", hooks=[make_hook(root, ["ok.txt"])])]
        },
        env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"},
    )
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    return result


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ok.txt").write_text("before\n")
        (root / "forbidden.txt").write_text("before\n")

        print("== check 1+2: hook denial, cost reporting")
        r = await run(
            "Replace the word 'before' with 'after' in BOTH ok.txt and forbidden.txt. "
            "If an edit is refused, say which file and why, then stop.",
            root,
            budget=0.50,
        )
        assert r is not None
        print("subtype:", r.subtype, "| is_error:", r.is_error, "| turns:", r.num_turns)
        print("total_cost_usd:", r.total_cost_usd)
        print("usage:", {k: v for k, v in (r.usage or {}).items() if "tokens" in k})
        print("ok.txt:", (root / "ok.txt").read_text().strip())
        print("forbidden.txt:", (root / "forbidden.txt").read_text().strip())
        print("agent said:", (r.result or "")[:300].replace("\n", " "))

        print("\n== check 3: tiny budget")
        r = await run("Read ok.txt and summarize it in one sentence.", root, budget=0.0001)
        assert r is not None
        print("subtype:", r.subtype, "| is_error:", r.is_error, "| cost:", r.total_cost_usd)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
