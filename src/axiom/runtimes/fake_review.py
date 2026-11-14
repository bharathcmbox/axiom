"""A scripted reviewer for tests and demos: no model, no network, no cost."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Any

from axiom.contracts import REVIEW_CHECKLISTS, InvalidResult, parse_review
from axiom.runtimes.base import EventCallback, Outcome
from axiom.runtimes.review import ReviewOutcome, ReviewRequest


class ReviewScenario(StrEnum):
    PASS = "pass"
    FAIL = "fail"  # one high finding
    PASS_WITH_HIGH = "pass_with_high"  # inconsistent: Axiom turns it into FAIL
    MALFORMED = "malformed"
    EXAMINED_NOTHING = "examined_nothing"  # a PASS with every checklist item not_applicable
    CRASH = "crash"
    BUDGET_EXCEEDED = "budget_exceeded"
    TRY_TO_EDIT = "try_to_edit"  # writes a file, as if the read-only hook had failed
    HANG = "hang"


@dataclass
class FakeReviewer:
    scenario: ReviewScenario = ReviewScenario.PASS
    finding_file: str = "agent/socagent/incidents.py"
    cost_usd: Decimal = Decimal("0.25")
    name: str = "fake-reviewer"
    _cancelled: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._cancelled.set()

    def _costed(self, outcome: Outcome, transcript: Path, **kwargs: Any) -> ReviewOutcome:
        return ReviewOutcome(
            outcome,
            cost_usd=self.cost_usd,
            input_tokens=9000,
            output_tokens=700,
            turns=5,
            transcript_path=transcript,
            **kwargs,
        )

    def run_review(
        self, request: ReviewRequest, on_event: EventCallback | None = None
    ) -> ReviewOutcome:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        transcript = request.output_dir / "transcript.jsonl"
        transcript.write_text(f'{{"scenario": "{self.scenario}", "role": "{request.role}"}}\n')
        if self.scenario == ReviewScenario.HANG:
            self._cancelled.wait()
            return ReviewOutcome(Outcome.CANCELLED, error="cancelled", transcript_path=transcript)
        if self.scenario == ReviewScenario.CRASH:
            return self._costed(Outcome.FAILED, transcript, error="reviewer crashed")
        if self.scenario == ReviewScenario.BUDGET_EXCEEDED:
            return self._costed(Outcome.BUDGET_EXCEEDED, transcript, error="budget exceeded")
        if self.scenario == ReviewScenario.TRY_TO_EDIT:
            (request.workspace / "REVIEWER_WAS_HERE.txt").write_text("edit\n")
        finding = {
            "severity": "high",
            "file": self.finding_file,
            "line": 42,
            "issue": f"{request.role}: planted defect",
            "required_change": "fix it",
        }
        items = list(REVIEW_CHECKLISTS[request.role])

        def checklist(finding_on: str | None = None, result: str = "ok") -> dict[str, Any]:
            return {
                i: {"result": "finding" if i == finding_on else result, "note": f"looked at {i}"}
                for i in items
            }

        raw: object = {
            ReviewScenario.PASS: {"verdict": "PASS", "summary": "looks right",
                                  "checklist": checklist(), "findings": []},
            ReviewScenario.TRY_TO_EDIT: {"verdict": "PASS", "summary": "edited",
                                         "checklist": checklist(), "findings": []},
            ReviewScenario.FAIL: {"verdict": "FAIL", "summary": "defect",
                                  "checklist": checklist(items[0]), "findings": [finding]},
            ReviewScenario.PASS_WITH_HIGH: {"verdict": "PASS", "summary": "ok?",
                                            "checklist": checklist(items[0]),
                                            "findings": [finding]},
            ReviewScenario.MALFORMED: {"verdict": "LGTM"},
            ReviewScenario.EXAMINED_NOTHING: {"verdict": "PASS", "summary": "fine",
                                              "checklist": checklist(result="not_applicable"),
                                              "findings": []},
        }[self.scenario]  # fmt: skip
        try:
            contract = parse_review(raw, request.role)
        except InvalidResult as e:
            return self._costed(Outcome.FAILED, transcript, error=f"invalid review contract: {e}")
        return self._costed(Outcome.COMPLETED, transcript, contract=contract)
