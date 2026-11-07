"""Planner runtimes: run a read-only planning agent and return its proposal.

Parallel to `runtimes/review.py`. The proposal stays raw here: the API alone decides whether it's
acceptable (ADR-011), so a shape problem reaches the next attempt the same way a rule problem does.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from axiom.runtimes.base import EventCallback, Outcome


@dataclass(frozen=True)
class PlanRequest:
    plan_id: int
    project_key: str
    specification: str
    plan_budget_usd: Decimal
    test_paths: list[str]
    workspace: Path  # a checkout of the default branch (read-only for the agent)
    output_dir: Path
    budget_usd: Decimal  # this run's limit
    feedback: list[str] = field(default_factory=list)
    previous_plan: dict[str, Any] | None = None
    model: str | None = None
    effort: str = "high"
    max_turns: int = 40


@dataclass(frozen=True)
class PlanOutcome:
    outcome: Outcome  # COMPLETED, FAILED, BUDGET_EXCEEDED or CANCELLED
    contract: dict[str, Any] | None = None
    error: str | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    transcript_path: Path | None = None
    cost_estimated: bool = False

    def cost_payload(self) -> dict[str, Any]:
        return {
            "usd": str(self.cost_usd) if self.cost_usd is not None else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated": self.cost_estimated,
        }


class PlanRuntime(Protocol):
    name: str

    def run_plan(
        self, request: PlanRequest, on_event: EventCallback | None = None
    ) -> PlanOutcome: ...

    def cancel(self) -> None: ...


def plan_request_to_dict(req: PlanRequest) -> dict[str, Any]:
    return {
        "mode": "plan",
        "plan_id": req.plan_id,
        "project_key": req.project_key,
        "specification": req.specification,
        "plan_budget_usd": str(req.plan_budget_usd),
        "test_paths": req.test_paths,
        "budget_usd": str(req.budget_usd),
        "feedback": req.feedback,
        "previous_plan": req.previous_plan,
        "model": req.model,
        "effort": req.effort,
        "max_turns": req.max_turns,
    }


def plan_request_from_dict(data: dict[str, Any], workspace: Path, output_dir: Path) -> PlanRequest:
    return PlanRequest(
        plan_id=int(data["plan_id"]),
        project_key=data["project_key"],
        specification=data["specification"],
        plan_budget_usd=Decimal(data["plan_budget_usd"]),
        test_paths=list(data["test_paths"]),
        workspace=workspace,
        output_dir=output_dir,
        budget_usd=Decimal(data["budget_usd"]),
        feedback=list(data.get("feedback", [])),
        previous_plan=data.get("previous_plan"),
        model=data.get("model"),
        effort=data.get("effort", "high"),
        max_turns=int(data.get("max_turns", 40)),
    )


def plan_outcome_to_dict(o: PlanOutcome) -> dict[str, Any]:
    return {
        "outcome": o.outcome.value,
        "contract": o.contract,
        "error": o.error,
        "cost_usd": str(o.cost_usd) if o.cost_usd is not None else None,
        "input_tokens": o.input_tokens,
        "output_tokens": o.output_tokens,
        "turns": o.turns,
        "transcript": o.transcript_path.name if o.transcript_path else None,
    }


def plan_outcome_from_dict(data: Any, output_dir: Path) -> PlanOutcome:
    """Written in the sandbox, so nothing about its shape is assumed."""
    if not isinstance(data, dict):
        return PlanOutcome(Outcome.FAILED, error="the sandbox's outcome was not an object")
    try:
        outcome = Outcome(str(data.get("outcome")))
        cost = data.get("cost_usd")
        cost_usd = Decimal(str(cost)) if cost is not None else None
    except ValueError, ArithmeticError:
        return PlanOutcome(Outcome.FAILED, error="the sandbox's outcome was not valid")
    contract = data.get("contract")
    error = data.get("error")
    if outcome == Outcome.COMPLETED and not isinstance(contract, dict):
        outcome, contract, error = Outcome.FAILED, None, error or "no plan was returned"
    transcript = data.get("transcript")
    return PlanOutcome(
        outcome=outcome,
        contract=contract if isinstance(contract, dict) else None,
        error=str(error) if error is not None else None,
        cost_usd=cost_usd,
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        turns=data.get("turns"),
        transcript_path=output_dir / str(transcript) if transcript else None,
    )
