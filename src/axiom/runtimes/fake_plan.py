"""A scripted planner for tests and demos: no model, no network, no cost."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Any

from axiom.runtimes.base import EventCallback, Outcome
from axiom.runtimes.plan import PlanOutcome, PlanRequest


class PlanScenario(StrEnum):
    GOOD = "good"
    BREAKS_THE_RULES = "breaks_the_rules"  # asks to change CI: well-formed, but refused
    MALFORMED = "malformed"
    CRASH = "crash"
    BUDGET_EXCEEDED = "budget_exceeded"
    TRY_TO_EDIT = "try_to_edit"  # writes a file, as if the read-only hook had failed
    HANG = "hang"


def good_plan(budget_usd: float = 2.0) -> dict[str, Any]:
    def task(ref: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "title": f"Task {ref}",
            "specification": f"Do {ref}.",
            "acceptance_criteria": [f"{ref} is done"],
            "role": "developer",
            "allowed_paths": [f"src/{ref}.py"],
            "priority": "P2",
            "budget_usd": budget_usd,
            "depends_on": [],
        } | kwargs

    return {
        "summary": "Build it, then test it.",
        "tasks": [
            task("impl"),
            task("qa", role="qa", allowed_paths=["tests/**"], depends_on=["impl"]),
        ],
        "risks": ["A scripted risk."],
    }


@dataclass
class FakePlanner:
    scenario: PlanScenario = PlanScenario.GOOD
    cost_usd: Decimal = Decimal("0.35")
    contract: dict[str, Any] | None = None
    name: str = "fake-planner"
    seen: list[PlanRequest] = field(default_factory=list)
    _cancelled: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._cancelled.set()

    def _costed(self, outcome: Outcome, transcript: Path, **kwargs: Any) -> PlanOutcome:
        return PlanOutcome(
            outcome,
            cost_usd=self.cost_usd,
            input_tokens=9000,
            output_tokens=700,
            turns=5,
            transcript_path=transcript,
            **kwargs,
        )

    def run_plan(self, request: PlanRequest, on_event: EventCallback | None = None) -> PlanOutcome:
        self.seen.append(request)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        transcript = request.output_dir / "transcript.jsonl"
        transcript.write_text(f'{{"scenario": "{self.scenario}"}}\n')
        if self.scenario == PlanScenario.HANG:
            self._cancelled.wait()
            return PlanOutcome(Outcome.CANCELLED, error="cancelled", transcript_path=transcript)
        if self.scenario == PlanScenario.CRASH:
            return self._costed(Outcome.FAILED, transcript, error="planner crashed")
        if self.scenario == PlanScenario.BUDGET_EXCEEDED:
            return self._costed(Outcome.BUDGET_EXCEEDED, transcript, error="budget exceeded")
        if self.scenario == PlanScenario.TRY_TO_EDIT:
            (request.workspace / "PLANNER_WAS_HERE.txt").write_text("edit\n")
        plan: dict[str, Any] = self.contract or good_plan()
        if self.scenario == PlanScenario.MALFORMED:
            plan = {"summary": "Not a plan", "approved": True}
        if self.scenario == PlanScenario.BREAKS_THE_RULES:
            plan = good_plan() | {
                "tasks": [good_plan()["tasks"][0] | {"allowed_paths": [".github/workflows/ci.yml"]}]
            }
        return self._costed(Outcome.COMPLETED, transcript, contract=plan)
