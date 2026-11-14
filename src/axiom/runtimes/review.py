"""Reviewer runtimes: run a read-only review agent on one commit and return a review contract.

Parallel to `runtimes/base.py` (developer runs). Kept separate because a review has different
inputs (a diff, the developer's claims, a reviewer role) and output (a ReviewContract).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from axiom.contracts import InvalidResult, ReviewContract, parse_review
from axiom.runtimes.base import EventCallback, Outcome

MAX_DIFF_CHARS = 120_000


@dataclass(frozen=True)
class ReviewRequest:
    task_key: str
    review_id: int
    role: str  # code_reviewer | security_reviewer
    title: str
    specification: str
    acceptance_criteria: list[str]
    allowed_paths: list[str]
    diff: str
    developer_result: dict[str, Any] | None
    workspace: Path  # checkout at the commit under review (read-only for the agent)
    output_dir: Path
    budget_usd: Decimal
    model: str | None = None
    effort: str = "high"
    max_turns: int = 40
    previous_findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewOutcome:
    outcome: Outcome  # COMPLETED, FAILED, BUDGET_EXCEEDED or CANCELLED
    contract: ReviewContract | None = None
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


class ReviewRuntime(Protocol):
    name: str

    def run_review(
        self, request: ReviewRequest, on_event: EventCallback | None = None
    ) -> ReviewOutcome: ...

    def cancel(self) -> None: ...


def truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return (
        diff[:MAX_DIFF_CHARS]
        + f"\n\n[diff truncated: {len(diff) - MAX_DIFF_CHARS} more characters; "
        + "read the files directly]\n"
    )


def review_request_to_dict(req: ReviewRequest) -> dict[str, Any]:
    return {
        "mode": "review",
        "task_key": req.task_key,
        "review_id": req.review_id,
        "role": req.role,
        "title": req.title,
        "specification": req.specification,
        "acceptance_criteria": req.acceptance_criteria,
        "allowed_paths": req.allowed_paths,
        "diff": req.diff,
        "developer_result": req.developer_result,
        "budget_usd": str(req.budget_usd),
        "model": req.model,
        "effort": req.effort,
        "max_turns": req.max_turns,
        "previous_findings": req.previous_findings,
    }


def review_request_from_dict(
    data: dict[str, Any], workspace: Path, output_dir: Path
) -> ReviewRequest:
    return ReviewRequest(
        task_key=data["task_key"],
        review_id=int(data["review_id"]),
        role=data["role"],
        title=data["title"],
        specification=data["specification"],
        acceptance_criteria=list(data["acceptance_criteria"]),
        allowed_paths=list(data["allowed_paths"]),
        diff=data["diff"],
        developer_result=data.get("developer_result"),
        workspace=workspace,
        output_dir=output_dir,
        budget_usd=Decimal(data["budget_usd"]),
        model=data.get("model"),
        effort=data.get("effort", "high"),
        max_turns=int(data.get("max_turns", 40)),
        previous_findings=list(data.get("previous_findings", [])),
    )


def review_outcome_to_dict(o: ReviewOutcome) -> dict[str, Any]:
    return {
        "outcome": o.outcome.value,
        "contract": o.contract.model_dump(mode="json") if o.contract else None,
        "error": o.error,
        "cost_usd": str(o.cost_usd) if o.cost_usd is not None else None,
        "input_tokens": o.input_tokens,
        "output_tokens": o.output_tokens,
        "turns": o.turns,
        "transcript": o.transcript_path.name if o.transcript_path else None,
    }


def review_outcome_from_dict(data: dict[str, Any], output_dir: Path, role: str) -> ReviewOutcome:
    """Parse what the sandbox wrote; the contract is re-validated (the sandbox is untrusted)."""
    outcome = Outcome(data["outcome"])
    contract, error = None, data.get("error")
    if data.get("contract") is not None:
        try:
            contract = parse_review(data["contract"], role)
        except InvalidResult as e:
            outcome, error = Outcome.FAILED, f"invalid review contract: {e}"
    if outcome == Outcome.COMPLETED and contract is None:
        outcome, error = Outcome.FAILED, error or "no review contract"
    transcript = data.get("transcript")
    return ReviewOutcome(
        outcome=outcome,
        contract=contract,
        error=error,
        cost_usd=Decimal(data["cost_usd"]) if data.get("cost_usd") is not None else None,
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        turns=data.get("turns"),
        transcript_path=output_dir / transcript if transcript else None,
    )
