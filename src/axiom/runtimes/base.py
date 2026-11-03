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


class Runtime(Protocol):
    """Runs one agent execution. `run` blocks until the agent finishes or is cancelled."""

    name: str

    def run(self, request: RunRequest, on_event: EventCallback | None = None) -> RunOutcome: ...

    def cancel(self) -> None: ...
