"""The runtime interface: everything a worker needs from "something that runs an agent".

A runtime gets a prepared workspace and a task, runs an agent in it, and reports what happened.
It never touches the Axiom API or GitHub; the worker harness does that (ADR-008).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from axiom.contracts import ResultContract


@dataclass(frozen=True)
class RunRequest:
    task_key: str
    title: str
    specification: str
    acceptance_criteria: list[str]
    allowed_paths: list[str]
    workspace: Path  # the repository checkout the agent works in
    output_dir: Path  # where the runtime writes transcript / raw output
    budget_usd: Decimal  # hard ceiling for this run
    model: str | None = None
    effort: str = "high"
    max_turns: int = 60
    project_guidance: str | None = None  # e.g. the repo's CLAUDE.md, passed as untrusted text
    previous_feedback: list[str] = field(default_factory=list)  # review findings, answers


class Outcome(StrEnum):
    COMPLETED = "completed"  # result contract with status=submitted
    NEEDS_INPUT = "needs_input"  # result contract with status=needs_input
    FAILED = "failed"  # error, crash, invalid result, turn limit
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunOutcome:
    outcome: Outcome
    result: ResultContract | None = None
    error: str | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    transcript_path: Path | None = None

    def cost_payload(self) -> dict[str, Any]:
        return {
            "usd": str(self.cost_usd) if self.cost_usd is not None else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


EventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str


class CommandRunner(Protocol):
    """Runs a shell command in a checkout. Production: a fresh sandbox (SandboxRuntime.exec)."""

    def exec(self, workdir: Path, outdir: Path, command: str, timeout: int) -> CommandResult: ...


class Runtime(Protocol):
    """Runs one agent execution. `run` blocks until the agent finishes or is cancelled."""

    name: str

    def run(self, request: RunRequest, on_event: EventCallback | None = None) -> RunOutcome: ...

    def cancel(self) -> None: ...


# ---- wire format between the harness and the sandbox (request.json / outcome.json) ---------


def request_to_dict(req: RunRequest) -> dict[str, Any]:
    """Everything but host paths; the sandbox sees its own mount points."""
    return {
        "task_key": req.task_key,
        "title": req.title,
        "specification": req.specification,
        "acceptance_criteria": req.acceptance_criteria,
        "allowed_paths": req.allowed_paths,
        "budget_usd": str(req.budget_usd),
        "model": req.model,
        "effort": req.effort,
        "max_turns": req.max_turns,
        "previous_feedback": req.previous_feedback,
    }


def request_from_dict(data: dict[str, Any], workspace: Path, output_dir: Path) -> RunRequest:
    return RunRequest(
        task_key=data["task_key"],
        title=data["title"],
        specification=data["specification"],
        acceptance_criteria=list(data["acceptance_criteria"]),
        allowed_paths=list(data["allowed_paths"]),
        workspace=workspace,
        output_dir=output_dir,
        budget_usd=Decimal(data["budget_usd"]),
        model=data.get("model"),
        effort=data.get("effort", "high"),
        max_turns=int(data.get("max_turns", 60)),
        previous_feedback=list(data.get("previous_feedback", [])),
    )


def outcome_to_dict(o: RunOutcome) -> dict[str, Any]:
    return {
        "outcome": o.outcome.value,
        "result": o.result.model_dump(mode="json") if o.result else None,
        "error": o.error,
        "cost_usd": str(o.cost_usd) if o.cost_usd is not None else None,
        "input_tokens": o.input_tokens,
        "output_tokens": o.output_tokens,
        "turns": o.turns,
        "transcript": o.transcript_path.name if o.transcript_path else None,
    }


def outcome_from_dict(data: dict[str, Any], output_dir: Path) -> RunOutcome:
    """Parse what the sandbox wrote. The result is re-validated: the sandbox is untrusted."""
    from axiom.contracts import InvalidResult, parse_result

    outcome = Outcome(data["outcome"])
    result = None
    error = data.get("error")
    if data.get("result") is not None:
        try:
            result = parse_result(data["result"])
        except InvalidResult as e:
            outcome, error = Outcome.FAILED, f"invalid result: {e}"
    if outcome in (Outcome.COMPLETED, Outcome.NEEDS_INPUT) and result is None:
        outcome, error = Outcome.FAILED, error or "no result"
    transcript = data.get("transcript")
    return RunOutcome(
        outcome=outcome,
        result=result,
        error=error,
        cost_usd=Decimal(data["cost_usd"]) if data.get("cost_usd") is not None else None,
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        turns=data.get("turns"),
        transcript_path=output_dir / transcript if transcript else None,
    )
