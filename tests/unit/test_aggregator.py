"""Phase 3 T3: the aggregator's decision table, as a pure function (no database)."""

from typing import Any

import pytest

from axiom.orchestrator.aggregator import decide

FINDING = {"severity": "high", "file": "a.py", "line": 3, "issue": "off by one",
           "required_change": "use <="}  # fmt: skip


def _task(rounds: int = 0, ci: str | None = None, ci_sha: str | None = None) -> dict[str, Any]:
    return {"review_rounds": rounds, "head_sha": "abc123", "ci_status": ci, "ci_sha": ci_sha,
            "ci_details": {"summary": "2 tests failed"}}  # fmt: skip


def _r(role: str, status: str, findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    contract = {"summary": f"{role} says {status}", "findings": findings or []}
    return {
        "role": role,
        "status": status,
        "contract": contract if status in ("PASS", "FAIL") else None,
    }


CODE, SEC = "code_reviewer", "security_reviewer"


@pytest.mark.parametrize(
    ("reviews", "task", "require_ci", "expected"),
    [
        # still waiting
        ([_r(CODE, "PENDING"), _r(SEC, "PENDING")], _task(), False, "wait"),
        ([_r(CODE, "PASS"), _r(SEC, "CLAIMED")], _task(), False, "wait"),
        # all pass
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(), False, "approve"),
        # all pass, CI required but pending / for another commit / green
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="pending", ci_sha="abc123"), True, "wait"),
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="success", ci_sha="old999"), True, "wait"),
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="success", ci_sha="abc123"), True,
         "approve"),
        # one FAIL is enough, even while the other review is still running
        ([_r(CODE, "FAIL", [FINDING]), _r(SEC, "CLAIMED")], _task(), False, "request_changes"),
        # CI red sends work back even if reviews pass; stale CI is ignored
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="failure", ci_sha="abc123"), True,
         "request_changes"),
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="failure", ci_sha="old999"), True, "wait"),
        # CI not required: its failure is informational only
        ([_r(CODE, "PASS"), _r(SEC, "PASS")], _task(ci="failure", ci_sha="abc123"), False,
         "approve"),
        # a review that could not be completed goes to a human
        ([_r(CODE, "ERROR"), _r(SEC, "PASS")], _task(), False, "escalate"),
        # third round with a FAIL: escalate instead of looping
        ([_r(CODE, "FAIL", [FINDING]), _r(SEC, "PASS")], _task(rounds=2), False, "escalate"),
        ([_r(CODE, "FAIL", [FINDING]), _r(SEC, "PASS")], _task(rounds=1), False,
         "request_changes"),
    ],
)  # fmt: skip
def test_decision_table(
    reviews: list[dict[str, Any]], task: dict[str, Any], require_ci: bool, expected: str
) -> None:
    assert decide(task, reviews, require_ci)[0] == expected


def test_request_changes_carries_findings_as_feedback() -> None:
    _, payload = decide(_task(ci="failure", ci_sha="abc123"),
                        [_r(CODE, "FAIL", [FINDING]), _r(SEC, "PASS")], True)  # fmt: skip
    assert payload["findings"] == [{"reviewer": CODE} | FINDING]
    assert "[high] a.py:3 off by one → use <=" in payload["feedback"]
    assert any(f.startswith("CI failed for abc123") for f in payload["feedback"])


def test_escalation_says_why() -> None:
    _, payload = decide(_task(rounds=2), [_r(CODE, "FAIL", [FINDING]), _r(SEC, "PASS")], False)
    assert "3 review rounds" in payload["reason"]
