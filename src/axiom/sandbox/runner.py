"""Sandbox entry point: `python -m axiom.sandbox.runner`.

Reads <out>/request.json, runs the Claude Agent SDK in <repo>, writes <out>/outcome.json and
<out>/transcript.jsonl. Has no Axiom or GitHub credentials; only ANTHROPIC_API_KEY.
If AXIOM_FAKE_SCENARIO is set, runs the FakeRuntime instead (container plumbing tests, no cost).
"""

import asyncio
import dataclasses
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.contracts import InvalidResult, parse_result, result_json_schema
from axiom.runtimes.base import (
    Outcome,
    RunOutcome,
    RunRequest,
    outcome_to_dict,
    request_from_dict,
)
from axiom.sandbox.hooks import EDIT_TOOLS, READ_TOOLS, make_guard
from axiom.sandbox.prompt import RULES, build_prompt

WORKSPACE = Path(os.environ.get("AXIOM_WORKSPACE", "/workspace/repo"))
OUTPUT = Path(os.environ.get("AXIOM_OUTPUT", "/workspace/out"))
BUDGET_HEADROOM = Decimal("0.8")  # the SDK's cost estimate can overshoot max_budget_usd
TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
DISALLOWED = ["WebFetch", "WebSearch", "Task", "NotebookEdit"]

QueryFn = Callable[..., AsyncIterator[Any]]


def read_guidance(workspace: Path) -> str | None:
    path = workspace / "CLAUDE.md"
    return path.read_text(errors="replace") if path.is_file() else None


def build_options(request: RunRequest) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    guard = make_guard(request.workspace, request.allowed_paths)
    return ClaudeAgentOptions(
        model=request.model,
        effort=request.effort,  # type: ignore[arg-type]
        cwd=str(request.workspace),
        system_prompt={"type": "preset", "preset": "claude_code", "append": RULES},
        tools=TOOLS,
        allowed_tools=TOOLS,
        disallowed_tools=DISALLOWED,
        permission_mode="dontAsk",
        setting_sources=[],  # never load the repository's .claude/settings.json
        max_turns=request.max_turns,
        max_budget_usd=float(request.budget_usd * BUDGET_HEADROOM),
        output_format={"type": "json_schema", "schema": result_json_schema()},
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="|".join(EDIT_TOOLS + READ_TOOLS), hooks=[guard])  # type: ignore[list-item]
            ]
        },
        env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"},
    )


def _jsonable(message: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(message) and not isinstance(message, type):
        body: Any = dataclasses.asdict(message)
    else:
        body = repr(message)
    return {"type": type(message).__name__, "data": body}


def to_outcome(result: Any, transcript: Path, raised: str | None) -> RunOutcome:
    """Map the SDK's final ResultMessage (or its absence) to a RunOutcome."""
    if result is None:
        return RunOutcome(Outcome.FAILED, error=raised or "agent produced no result",
                          transcript_path=transcript)  # fmt: skip
    usage = result.usage or {}
    tokens_in = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))  # fmt: skip
    common: dict[str, Any] = {
        "cost_usd": Decimal(str(result.total_cost_usd))
        if result.total_cost_usd is not None
        else None,
        "input_tokens": tokens_in or None,
        "output_tokens": usage.get("output_tokens"),
        "turns": result.num_turns,
        "transcript_path": transcript,
    }
    if result.subtype == "error_max_budget_usd":
        return RunOutcome(Outcome.BUDGET_EXCEEDED, error="budget exceeded", **common)
    if result.is_error or result.subtype != "success":
        detail = "; ".join(result.errors or []) or raised or result.subtype
        return RunOutcome(Outcome.FAILED, error=f"{result.subtype}: {detail}", **common)
    try:
        contract = parse_result(result.structured_output)
    except InvalidResult as e:
        return RunOutcome(Outcome.FAILED, error=f"invalid result: {e}", **common)
    outcome = Outcome.NEEDS_INPUT if contract.status == "needs_input" else Outcome.COMPLETED
    return RunOutcome(outcome, result=contract, **common)


async def run_agent(request: RunRequest, query_fn: QueryFn | None = None) -> RunOutcome:
    from claude_agent_sdk import ResultError, ResultMessage, query

    run_query = query_fn or query
    request.output_dir.mkdir(parents=True, exist_ok=True)
    transcript = request.output_dir / "transcript.jsonl"
    prompt = build_prompt(request, read_guidance(request.workspace))
    final, raised = None, None
    with transcript.open("w") as log:
        try:
            async for message in run_query(prompt=prompt, options=build_options(request)):
                log.write(json.dumps(_jsonable(message), default=str) + "\n")
                log.flush()  # a killed run's transcript is all that says what it cost
                if isinstance(message, ResultMessage):
                    final = message
        except ResultError as e:  # yielded after the error ResultMessage (see spike notes)
            raised = f"{e.subtype}: {'; '.join(e.errors or [])}"
        except Exception as e:  # SDK/CLI crash: report, never hang the harness
            raised = f"{type(e).__name__}: {e}"
    return to_outcome(final, transcript, raised)


def main() -> int:
    data = json.loads((OUTPUT / "request.json").read_text())
    if data.get("mode") == "review":
        return main_review(data)
    request = request_from_dict(data, WORKSPACE, OUTPUT)
    scenario = os.environ.get("AXIOM_FAKE_SCENARIO")
    if scenario:
        from axiom.runtimes.fake import FakeRuntime, Scenario

        outcome = FakeRuntime(scenario=Scenario(scenario)).run(request)
    else:
        outcome = asyncio.run(run_agent(request))
    (OUTPUT / "outcome.json").write_text(json.dumps(outcome_to_dict(outcome), indent=2))
    return 0


# ---- review mode (Phase 3) -------------------------------------------------------------------

REVIEW_TOOLS = ["Read", "Glob", "Grep"]
REVIEW_DISALLOWED = [
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "Task",
]
REVIEW_SYSTEM = (
    "You are an independent code reviewer. You can read and search files; you cannot change "
    "anything or run commands. Return your verdict as the structured review."
)


def build_review_options(request: Any) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from axiom.contracts import review_json_schema
    from axiom.sandbox.hooks import EDIT_TOOLS, make_review_guard

    guard = make_review_guard(request.workspace)
    matcher = HookMatcher(
        matcher="|".join(EDIT_TOOLS + tuple(REVIEW_TOOLS)),
        hooks=[guard],  # type: ignore[list-item]
    )
    return ClaudeAgentOptions(
        model=request.model,
        effort=request.effort,
        cwd=str(request.workspace),
        system_prompt={"type": "preset", "preset": "claude_code", "append": REVIEW_SYSTEM},
        tools=REVIEW_TOOLS,
        allowed_tools=REVIEW_TOOLS,
        disallowed_tools=REVIEW_DISALLOWED,
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=request.max_turns,
        max_budget_usd=float(request.budget_usd * BUDGET_HEADROOM),
        output_format={"type": "json_schema", "schema": review_json_schema()},
        hooks={"PreToolUse": [matcher]},
        env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"},
    )


def to_review_outcome(result: Any, transcript: Path, raised: str | None) -> Any:
    """Like to_outcome, but the success path parses a ReviewContract."""
    from axiom.contracts import parse_review
    from axiom.runtimes.review import ReviewOutcome

    if result is None or result.is_error or result.subtype != "success":
        base = to_outcome(result, transcript, raised)
        return ReviewOutcome(
            base.outcome,
            error=base.error,
            cost_usd=base.cost_usd,
            input_tokens=base.input_tokens,
            output_tokens=base.output_tokens,
            turns=base.turns,
            transcript_path=transcript,
        )
    usage = result.usage or {}
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    cost = result.total_cost_usd
    common: dict[str, Any] = {
        "cost_usd": Decimal(str(cost)) if cost is not None else None,
        "input_tokens": sum(int(usage.get(k) or 0) for k in keys) or None,
        "output_tokens": usage.get("output_tokens"),
        "turns": result.num_turns,
        "transcript_path": transcript,
    }
    try:
        contract = parse_review(result.structured_output)
    except InvalidResult as e:
        return ReviewOutcome(Outcome.FAILED, error=f"invalid review contract: {e}", **common)
    return ReviewOutcome(Outcome.COMPLETED, contract=contract, **common)


async def run_review(request: Any, query_fn: QueryFn | None = None) -> Any:
    from claude_agent_sdk import ResultError, ResultMessage, query

    from axiom.sandbox.review_prompt import build_review_prompt

    run_query = query_fn or query
    request.output_dir.mkdir(parents=True, exist_ok=True)
    transcript = request.output_dir / "transcript.jsonl"
    prompt = build_review_prompt(request, read_guidance(request.workspace))
    final, raised = None, None
    with transcript.open("w") as log:
        try:
            async for message in run_query(prompt=prompt, options=build_review_options(request)):
                log.write(json.dumps(_jsonable(message), default=str) + "\n")
                log.flush()  # a killed run's transcript is all that says what it cost
                if isinstance(message, ResultMessage):
                    final = message
        except ResultError as e:
            raised = f"{e.subtype}: {'; '.join(e.errors or [])}"
        except Exception as e:
            raised = f"{type(e).__name__}: {e}"
    return to_review_outcome(final, transcript, raised)


def main_review(data: dict[str, Any]) -> int:
    from axiom.runtimes.review import review_outcome_to_dict, review_request_from_dict

    request = review_request_from_dict(data, WORKSPACE, OUTPUT)
    scenario = os.environ.get("AXIOM_FAKE_REVIEW")
    if scenario:
        from axiom.runtimes.fake_review import FakeReviewer, ReviewScenario

        outcome = FakeReviewer(scenario=ReviewScenario(scenario)).run_review(request)
    else:
        outcome = asyncio.run(run_review(request))
    (OUTPUT / "review_outcome.json").write_text(
        json.dumps(review_outcome_to_dict(outcome), indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
