"""Phase 3 T3: review requests, claims, verdicts, leases, the aggregator, CI, and overrides."""

import threading
from decimal import Decimal
from typing import Any

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import Forbidden, InvalidInput, LeaseExpired, NotExecutionOwner
from axiom.orchestrator.aggregator import record_ci
from axiom.orchestrator.reviews import (
    claim_review,
    expire_review_leases,
    fail_review_run,
    list_reviews,
    review_heartbeat,
    submit_review,
)
from axiom.orchestrator.tasks import get_task
from axiom.registry.agents import register_agent
from axiom.state.db import Conn, connect
from tests.integration.helpers import DEV1, HUMAN, do, drive_to, events_for

C, S = TaskCommand, TaskState
REV = Actor(ActorKind.AGENT, "REV-01")
SEC = Actor(ActorKind.AGENT, "SEC-01")
SYNC = Actor(ActorKind.SYSTEM, "github-sync")
PASS = {"verdict": "PASS", "summary": "looks right", "findings": []}
BUG = {
    "severity": "high",
    "file": "agent/socagent/incidents.py",
    "line": 42,
    "issue": "groups alerts from different hosts",
    "required_change": "key by host too",
}
FAIL = {"verdict": "FAIL", "summary": "grouping bug", "findings": [BUG]}


def _in_review(db: Conn, commit: str = "abc123") -> str:
    key = drive_to(db, S.IN_PROGRESS)
    do(db, key, C.SUBMIT, payload={"summary": "done", "commit": commit})
    return key


def _claim_both(db: Conn) -> tuple[int, int]:
    code, sec = claim_review(db, REV), claim_review(db, SEC)
    assert code is not None and sec is not None
    return code.review_id, sec.review_id


def _expire(db: Conn, review_id: int) -> None:
    db.execute("UPDATE reviews SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
               (review_id,))  # fmt: skip


def _types(db: Conn, key: str) -> list[str]:
    return [e["type"] for e in events_for(db, key)]


# ---- requests and claims --------------------------------------------------------------------


def test_submit_requests_one_review_per_role(org: Conn) -> None:
    key = _in_review(org, commit="abc123")
    task = get_task(org, key)
    reviews = list_reviews(org, task.id)
    assert [(r["role"], r["status"], r["round"], r["head_sha"]) for r in reviews] == [
        ("code_reviewer", "PENDING", 1, "abc123"),
        ("security_reviewer", "PENDING", 1, "abc123"),
    ]
    assert task.head_sha == "abc123"


def test_reviewers_claim_only_their_role_and_get_the_task(org: Conn) -> None:
    key = _in_review(org)
    code = claim_review(org, REV)
    assert code is not None and code.role == "code_reviewer"
    assert code.task["key"] == key and code.developer_result is not None
    assert code.developer_result["summary"] == "done"
    # Half of the default $3 task budget: two reviews of this round are waiting to start.
    assert code.run_budget_usd == Decimal("1.50")
    assert claim_review(org, REV) is None  # the only code review is taken
    sec = claim_review(org, SEC)
    assert sec is not None and sec.role == "security_reviewer"


def test_developers_and_humans_cannot_claim_reviews(org: Conn) -> None:
    _in_review(org)
    with pytest.raises(Forbidden, match="not a reviewer"):
        claim_review(org, DEV1)
    with pytest.raises(Forbidden):
        claim_review(org, HUMAN)


def test_two_reviewers_racing_get_the_review_once(org: Conn, db_url: str) -> None:
    register_agent(org, HUMAN, key="REV-02", role="code_reviewer", runtime="claude_code")
    _in_review(org)
    barrier, results = threading.Barrier(2), []

    def claim(name: str) -> None:
        with connect(db_url) as c:
            barrier.wait()
            results.append(claim_review(c, Actor(ActorKind.AGENT, name)))

    threads = [threading.Thread(target=claim, args=(n,)) for n in ("REV-01", "REV-02")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1


# ---- verdicts and the aggregator --------------------------------------------------------------


def test_two_passes_approve_the_task(org: Conn) -> None:
    key = _in_review(org)
    code, sec = _claim_both(org)
    assert submit_review(org, REV, code, PASS) == "wait"
    assert get_task(org, key).state == S.IN_REVIEW
    assert submit_review(org, SEC, sec, PASS) == "approve"
    assert get_task(org, key).state == S.AWAITING_MERGE
    approved = next(e for e in events_for(org, key) if e["type"] == "REVIEW_APPROVED")
    assert approved["actor"] == "system:aggregator"


def test_one_fail_sends_work_back_at_once_with_findings(org: Conn) -> None:
    key = _in_review(org)
    code, _ = _claim_both(org)
    assert submit_review(org, REV, code, FAIL) == "request_changes"
    task = get_task(org, key)
    assert (task.state, task.review_rounds) == (S.READY, 1)
    reopened = [e for e in events_for(org, key) if e["type"] == "TASK_REOPENED"][-1]
    assert reopened["payload"]["findings"][0]["issue"] == BUG["issue"]
    assert any("key by host too" in f for f in reopened["payload"]["feedback"])
    statuses = {r["role"]: r["status"] for r in list_reviews(org, task.id)}
    assert statuses == {"code_reviewer": "FAIL", "security_reviewer": "SUPERSEDED"}


def test_pass_with_a_high_finding_counts_as_fail(org: Conn) -> None:
    key = _in_review(org)
    code, _ = _claim_both(org)
    assert submit_review(org, REV, code, PASS | {"findings": [BUG]}) == "request_changes"
    submitted = [e for e in events_for(org, key) if e["type"] == "REVIEW_SUBMITTED"][-1]
    assert submitted["payload"]["verdict"] == "FAIL"
    assert "treated as FAIL" in submitted["payload"]["policy_override"]


def test_low_findings_do_not_block(org: Conn) -> None:
    key = _in_review(org)
    code, sec = _claim_both(org)
    nit = BUG | {"severity": "low"}
    submit_review(org, REV, code, PASS | {"findings": [nit]})
    submit_review(org, SEC, sec, PASS)
    assert get_task(org, key).state == S.AWAITING_MERGE


def test_second_round_gets_fresh_reviews(org: Conn) -> None:
    key = _in_review(org, commit="sha-1")
    code, _ = _claim_both(org)
    submit_review(org, REV, code, FAIL)
    do(org, key, C.ASSIGN)
    do(org, key, C.START)
    do(org, key, C.SUBMIT, payload={"summary": "fixed", "commit": "sha-2"})
    task = get_task(org, key)
    round2 = [r for r in list_reviews(org, task.id) if r["round"] == 2]
    assert [(r["status"], r["head_sha"]) for r in round2] == [("PENDING", "sha-2")] * 2


def test_third_failed_round_escalates_to_a_human(org: Conn) -> None:
    key = _in_review(org)
    for round_ in (1, 2, 3):
        code, _ = _claim_both(org)
        decision = submit_review(org, REV, code, FAIL)
        if round_ < 3:
            assert decision == "request_changes"
            do(org, key, C.ASSIGN)
            do(org, key, C.START)
            do(org, key, C.SUBMIT, payload={"summary": f"try {round_ + 1}"})
    assert decision == "escalate"
    task = get_task(org, key)
    assert (task.state, task.review_rounds) == (S.WAITING_FOR_HUMAN, 2)
    escalated = [e for e in events_for(org, key) if e["type"] == "TASK_ESCALATED"][-1]
    assert "3 review rounds" in escalated["payload"]["reason"]


# ---- leases, failed runs, ownership -----------------------------------------------------------


def test_only_the_holder_with_a_live_lease_can_submit(org: Conn) -> None:
    _in_review(org)
    code, _ = _claim_both(org)
    with pytest.raises(NotExecutionOwner):
        submit_review(org, SEC, code, PASS)
    with pytest.raises(NotExecutionOwner):
        submit_review(org, DEV1, code, PASS)
    review_heartbeat(org, REV, code, lease_seconds=60)
    _expire(org, code)
    with pytest.raises(LeaseExpired):
        submit_review(org, REV, code, PASS)


def test_invalid_contracts_are_rejected_without_changes(org: Conn) -> None:
    key = _in_review(org)
    code, _ = _claim_both(org)
    before = _types(org, key)
    for bad in ({"verdict": "MAYBE", "summary": "x"}, {"verdict": "PASS", "summary": ""},
                {"verdict": "FAIL", "summary": "x", "findings": [{"severity": "huge"}]},
                PASS | {"approve_anyway": True}):  # fmt: skip
        with pytest.raises(InvalidInput):
            submit_review(org, REV, code, bad)
    assert _types(org, key) == before


def test_expired_review_lease_returns_to_pending_then_errors_and_escalates(org: Conn) -> None:
    key = _in_review(org)
    for attempt in (1, 2, 3):
        claimed = claim_review(org, REV)
        assert claimed is not None
        _expire(org, claimed.review_id)
        assert expire_review_leases(org) == [claimed.review_id]
        assert expire_review_leases(org) == []  # idempotent
        if attempt < 3:
            assert get_task(org, key).state == S.IN_REVIEW
    assert get_task(org, key).state == S.WAITING_FOR_HUMAN
    assert "REVIEW_TIMED_OUT" in _types(org, key) and "TASK_ESCALATED" in _types(org, key)


def test_failed_reviewer_runs_are_retried_then_escalated(org: Conn) -> None:
    key = _in_review(org)
    for _ in range(3):
        claimed = claim_review(org, REV)
        assert claimed is not None
        decision = fail_review_run(org, REV, claimed.review_id, "sandbox crashed",
                                   {"cost": {"usd": "0.05"}})  # fmt: skip
    assert decision == "escalate"
    task = get_task(org, key)
    assert task.state == S.WAITING_FOR_HUMAN
    assert task.budget_spent_usd >= Decimal("0.15")  # failed review runs still cost money


# ---- CI gate ------------------------------------------------------------------------------------


@pytest.fixture
def ci_required(org: Conn) -> Conn:
    org.execute("UPDATE projects SET require_ci = true")
    return org


def test_ci_must_be_green_for_the_head_commit(ci_required: Conn) -> None:
    db = ci_required
    key = _in_review(db, commit="abc123")
    code, sec = _claim_both(db)
    submit_review(db, REV, code, PASS)
    assert submit_review(db, SEC, sec, PASS) == "wait"  # CI unknown
    assert record_ci(db, SYNC, key, sha="abc123", status="pending", details={}) == "wait"
    assert record_ci(db, SYNC, key, sha="old999", status="success", details={}) == "wait"
    assert record_ci(db, SYNC, key, sha="abc123", status="success", details={}) == "approve"
    assert get_task(db, key).state == S.AWAITING_MERGE


def test_red_ci_sends_work_back_even_before_reviews_finish(ci_required: Conn) -> None:
    db = ci_required
    key = _in_review(db, commit="abc123")
    details = {"summary": "2 failed", "url": "https://ci/run/1"}
    assert record_ci(db, SYNC, key, sha="abc123", status="failure", details=details) == (
        "request_changes"
    )
    reopened = [e for e in events_for(db, key) if e["type"] == "TASK_REOPENED"][-1]
    assert reopened["payload"]["ci_failed"] is True
    assert any("2 failed" in f for f in reopened["payload"]["feedback"])
    assert record_ci(db, SYNC, key, sha="abc123", status="failure", details=details) == "unchanged"


def test_new_submission_resets_ci(ci_required: Conn) -> None:
    db = ci_required
    key = _in_review(db, commit="sha-1")
    record_ci(db, SYNC, key, sha="sha-1", status="failure", details={})
    do(db, key, C.ASSIGN)
    do(db, key, C.START)
    do(db, key, C.SUBMIT, payload={"summary": "fixed", "commit": "sha-2"})
    task = get_task(db, key)
    assert (task.head_sha, task.ci_status) == ("sha-2", None)


# ---- overrides and cleanup ----------------------------------------------------------------------


@pytest.mark.parametrize("command", [C.APPROVE, C.REQUEST_CHANGES, C.CANCEL])
def test_human_overrides_supersede_open_reviews(org: Conn, command: TaskCommand) -> None:
    key = _in_review(org)
    claim_review(org, REV)
    do(org, key, command)
    statuses = {r["status"] for r in list_reviews(org, get_task(org, key).id)}
    assert statuses == {"SUPERSEDED"}


def test_reviews_cost_counts_toward_the_task(org: Conn) -> None:
    key = _in_review(org)
    code, sec = _claim_both(org)
    cost: dict[str, Any] = {"cost": {"usd": "0.40", "input_tokens": 9000, "output_tokens": 800}}
    submit_review(org, REV, code, PASS, cost)
    submit_review(org, SEC, sec, PASS, cost)
    assert get_task(org, key).budget_spent_usd == Decimal("0.80")


def test_superseded_review_still_reports_its_cost_but_not_its_verdict(org: Conn) -> None:
    """Found in the live run: the second reviewer finished after the first one's FAIL had already
    sent the task back; its $0.30 vanished from the budget."""
    key = _in_review(org)
    code, sec = _claim_both(org)
    submit_review(org, REV, code, FAIL)  # supersedes the security review
    cost: dict[str, Any] = {"cost": {"usd": "0.30", "input_tokens": 9000, "output_tokens": 500}}
    assert submit_review(org, SEC, sec, PASS, cost) == "superseded"
    task = get_task(org, key)
    assert task.state == S.READY and task.budget_spent_usd == Decimal("0.30")
    review = next(r for r in list_reviews(org, task.id) if r["id"] == sec)
    assert (review["status"], review["verdict"], review["input_tokens"]) == (
        "SUPERSEDED",
        None,
        9000,
    )
    assert submit_review(org, SEC, sec, PASS, cost) == "superseded"  # a repeat cannot double-count
    assert get_task(org, key).budget_spent_usd == Decimal("0.30")
    late = [e for e in events_for(org, key) if e["type"] == "REVIEW_COST_RECORDED"]
    assert late[0]["payload"]["late_verdict"] == "PASS" and late[0]["to_state"] is None
    with pytest.raises(NotExecutionOwner):
        submit_review(org, REV, sec, PASS, cost)  # only the holder may report
    assert fail_review_run(org, SEC, sec, "crashed", cost) == "superseded"


def test_a_decided_review_cannot_be_submitted_again(org: Conn) -> None:
    from axiom.domain.review_machine import IllegalReviewTransition

    _in_review(org)
    code, _ = _claim_both(org)
    submit_review(org, REV, code, PASS)
    with pytest.raises(IllegalReviewTransition):
        submit_review(org, REV, code, FAIL)
