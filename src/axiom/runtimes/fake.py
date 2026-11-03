"""A scripted runtime for tests and demos: no model, no network, no cost.

Each scenario does something a real agent could do, including misbehaving (editing files it
was not allowed to, returning a malformed result), so the harness's checks can be tested.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from threading import Event
from typing import Any

from axiom.contracts import InvalidResult, parse_result
from axiom.runtimes.base import EventCallback, Outcome, RunOutcome, RunRequest


class Scenario(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    BUDGET_EXCEEDED = "budget_exceeded"
    NEEDS_INPUT = "needs_input"
    EDIT_OUTSIDE_ALLOWED = "edit_outside_allowed"  # as if the edit hook had failed
    LEAK_SECRET = "leak_secret"  # writes something that looks like a credential
    MALFORMED_RESULT = "malformed_result"
    HANG = "hang"  # blocks until cancelled


@dataclass
class FakeRuntime:
    scenario: Scenario = Scenario.SUCCESS
    edits: dict[str, str] = field(default_factory=dict)  # path -> content, within allowed paths
    cost_usd: Decimal = Decimal("0.42")
    name: str = "fake"
    _cancelled: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self, request: RunRequest, on_event: EventCallback | None = None) -> RunOutcome:
        emit = on_event or (lambda e: None)
        emit({"type": "started", "runtime": self.name, "task": request.task_key})
        request.output_dir.mkdir(parents=True, exist_ok=True)
        transcript = request.output_dir / "transcript.jsonl"
        transcript.write_text(json.dumps({"scenario": self.scenario}) + "\n")

        if self.scenario == Scenario.HANG:
            self._cancelled.wait()
            return RunOutcome(Outcome.CANCELLED, error="cancelled", transcript_path=transcript)
        if self.scenario == Scenario.FAIL:
            return self._costed(
                Outcome.FAILED, error="agent crashed", turns=3, transcript_path=transcript
            )
        if self.scenario == Scenario.BUDGET_EXCEEDED:
            spent = request.budget_usd + Decimal("0.01")
            return RunOutcome(
                Outcome.BUDGET_EXCEEDED,
                error="budget exceeded",
                cost_usd=spent,
                transcript_path=transcript,
            )

        edits = dict(self.edits) or self._default_edits(request)
        if self.scenario == Scenario.EDIT_OUTSIDE_ALLOWED:
            edits["NOT_ALLOWED.txt"] = "this file is outside allowed_paths\n"
        if self.scenario == Scenario.LEAK_SECRET:
            fake_key = "sk-ant-api03-" + "X" * 40
            edits[next(iter(edits))] = f"ANTHROPIC_API_KEY={fake_key}\n"
        for rel, content in edits.items():
            path = request.workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            emit({"type": "edit", "path": rel})

        if self.scenario == Scenario.MALFORMED_RESULT:
            raw: object = {"status": "done!", "summary": ""}
        elif self.scenario == Scenario.NEEDS_INPUT:
            raw = {"status": "needs_input", "summary": "blocked", "question": "Which DB?"}
        else:
            raw = {
                "status": "submitted",
                "summary": f"Implemented {request.title}",
                "files_changed": sorted(edits),
                "criteria": [
                    {"criterion": c, "met": True, "evidence": "fake runtime"}
                    for c in request.acceptance_criteria
                ],
            }
        try:
            result = parse_result(raw)
        except InvalidResult as e:
            return self._costed(
                Outcome.FAILED, error=f"invalid result: {e}", transcript_path=transcript
            )
        outcome = Outcome.NEEDS_INPUT if result.status == "needs_input" else Outcome.COMPLETED
        return self._costed(outcome, result=result, turns=4, transcript_path=transcript)

    def _costed(self, outcome: Outcome, **kwargs: Any) -> RunOutcome:
        return RunOutcome(
            outcome, cost_usd=self.cost_usd, input_tokens=1200, output_tokens=300, **kwargs
        )

    @staticmethod
    def _default_edits(request: RunRequest) -> dict[str, str]:
        exact = [p for p in request.allowed_paths if not any(ch in p for ch in "*?[")]
        target = exact[0] if exact else "AXIOM_FAKE_CHANGE.md"
        return {target: f"# change for {request.task_key}\n"}
