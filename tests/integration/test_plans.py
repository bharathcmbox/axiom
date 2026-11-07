"""Phase 4 T7: plans over real Postgres: claims, attempts, approval, and who may do what."""

import threading
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from axiom.api.app import create_app
from axiom.config import Settings, get_settings
from axiom.domain.plan_machine import IllegalPlanTransition, PlanActorNotPermitted
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import (
    AssignmentRejected,
    Forbidden,
    InvalidInput,
    LeaseExpired,
    NotExecutionOwner,
    NotFound,
)
from axiom.orchestrator import budgets, plans
from axiom.orchestrator.tasks import get_task, list_tasks
from axiom.registry.projects import set_test_paths
from axiom.report import build_report
from axiom.state.db import Conn, connect
from axiom.state.invariants import check_invariants
from tests.conftest import TEST_TOKENS, bearer
from tests.integration.helpers import DEV1, HUMAN, do, new_task

EM = Actor(ActorKind.AGENT, "EM-01")
ARCH = Actor(ActorKind.AGENT, "ARCH-01")


def _task(ref: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "title": f"Task {ref}",
        "specification": f"Do {ref}.",
        "acceptance_criteria": [f"{ref} is done"],
        "role": "developer",
        "allowed_paths": [f"agent/{ref}.py"],
        "priority": "P1",
        "budget_usd": 2.0,
        "depends_on": [],
    } | kwargs


GOOD = {
    "summary": "Build it, then test it.",
    "tasks": [
        _task("impl"),
        _task("cli"),
        _task("qa", role="qa", allowed_paths=["agent/tests/**"], depends_on=["impl", "cli"]),
    ],
    "risks": ["The CLI may change."],
}
BAD = {
    "summary": "Touches CI.",
    "tasks": [_task("impl", allowed_paths=[".github/workflows/ci.yml"])],
    "risks": [],
}


@pytest.fixture
def db_plans(org: Conn) -> Conn:
    set_test_paths(org, HUMAN, "AX", ["agent/tests/**"])
    return org


def _requested(db: Conn, budget: str = "10") -> int:
    return int(
        plans.request_plan(
            db, HUMAN, project_key="AX", specification="Build it.", budget_usd=Decimal(budget)
        )["id"]
    )


def _claimed(db: Conn, budget: str = "10") -> int:
    plan_id = _requested(db, budget)
    assignment = plans.claim_plan(db, EM)
    assert assignment is not None and assignment.plan_id == plan_id
    return plan_id


def _proposed(db: Conn, contract: dict[str, Any] = GOOD, budget: str = "10") -> int:
    plan_id = _claimed(db, budget)
    assert plans.submit_plan(db, EM, plan_id, contract, {"cost": {"usd": "0.40"}}) == "PROPOSED"
    return plan_id


def _snapshot(db: Conn, plan_id: int) -> dict[str, Any]:
    """Everything a command could change, for illegal-path tests."""
    plan = db.execute("SELECT * FROM plans WHERE id = %s", (plan_id,)).fetchone()
    events = db.execute("SELECT count(*) AS n FROM events").fetchone()
    tasks = db.execute("SELECT count(*) AS n FROM tasks").fetchone()
    return {"plan": plan, "events": events, "tasks": tasks}


def _types(db: Conn, plan_id: int) -> list[str]:
    return [e["type"] for e in plans.plan_events(db, plan_id)]


# ---- requesting -----------------------------------------------------------------------------


def test_a_human_requests_a_plan(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    plan = plans.get_plan(db_plans, plan_id)
    assert plan["status"] == "REQUESTED" and plan["created_by"] == str(HUMAN)
    assert _types(db_plans, plan_id) == ["PLAN_REQUESTED"]


def test_requesting_a_plan_creates_no_tasks(db_plans: Conn) -> None:
    _requested(db_plans)
    assert list_tasks(db_plans) == []


@pytest.mark.parametrize("actor", [DEV1, EM, Actor(ActorKind.SYSTEM, "x")])
def test_only_a_human_requests_a_plan(db_plans: Conn, actor: Actor) -> None:
    with pytest.raises(Forbidden):
        plans.request_plan(
            db_plans, actor, project_key="AX", specification="s", budget_usd=Decimal(1)
        )
    assert plans.list_plans(db_plans) == []


@pytest.mark.parametrize(("spec", "budget"), [("  ", "1"), ("s", "0"), ("s", "-1")])
def test_a_plan_needs_a_spec_and_a_budget(db_plans: Conn, spec: str, budget: str) -> None:
    with pytest.raises(InvalidInput):
        plans.request_plan(
            db_plans, HUMAN, project_key="AX", specification=spec, budget_usd=Decimal(budget)
        )
    assert plans.list_plans(db_plans) == []


def test_an_unknown_project_or_plan_is_not_found(db_plans: Conn) -> None:
    with pytest.raises(NotFound):
        plans.request_plan(
            db_plans, HUMAN, project_key="NOPE", specification="s", budget_usd=Decimal(1)
        )
    with pytest.raises(NotFound):
        plans.get_plan(db_plans, 999)


# ---- claiming -------------------------------------------------------------------------------


def test_claiming_gives_the_planner_what_it_needs(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    a = plans.claim_plan(db_plans, EM)
    assert a is not None and a.plan_id == plan_id and a.project_key == "AX"
    assert a.test_paths == ["agent/tests/**"] and a.budget_usd == Decimal(10)
    assert a.run_budget_usd == get_settings().plan_run_budget_usd
    assert plans.get_plan(db_plans, plan_id)["status"] == "CLAIMED"


def test_nothing_to_claim(db_plans: Conn) -> None:
    assert plans.claim_plan(db_plans, EM) is None


@pytest.mark.parametrize("actor", [DEV1, ARCH, HUMAN])
def test_only_a_planner_claims(db_plans: Conn, actor: Actor) -> None:
    plan_id = _requested(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(Forbidden):
        plans.claim_plan(db_plans, actor)
    assert _snapshot(db_plans, plan_id) == before


def test_a_planner_holds_one_plan_at_a_time(db_plans: Conn) -> None:
    _claimed(db_plans)
    second = _requested(db_plans)
    before = _snapshot(db_plans, second)
    with pytest.raises(AssignmentRejected, match="capacity"):
        plans.claim_plan(db_plans, EM)
    assert _snapshot(db_plans, second) == before


def test_two_claims_at_once_never_take_the_same_plan(db_plans: Conn, db_url: str) -> None:
    db_plans.execute("UPDATE agents SET max_concurrent = 5 WHERE key = 'EM-01'")
    _requested(db_plans)
    barrier, got = threading.Barrier(4), []

    def claim() -> None:
        with connect(db_url) as conn:
            barrier.wait()
            got.append(plans.claim_plan(conn, EM))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(a is not None for a in got) == 1


def test_a_claim_reserves_from_the_day(db_plans: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal("0.60"))
    _claimed(db_plans)
    assert budgets.reserved(db_plans) == Decimal("0.60")  # capped by the day, not the $1 per run


def test_no_planning_when_the_day_is_spoken_for(
    db_plans: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal(1))
    db_plans.execute("UPDATE agents SET max_concurrent = 5 WHERE key = 'EM-01'")
    _claimed(db_plans)
    second = _requested(db_plans)
    before = _snapshot(db_plans, second)
    with pytest.raises(AssignmentRejected, match="reserved"):
        plans.claim_plan(db_plans, EM)
    assert _snapshot(db_plans, second) == before


def test_no_planning_on_too_little(db_plans: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run below the minimum would burn money and fail, so it waits instead."""
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal("0.40"))
    plan_id = _requested(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(AssignmentRejected):
        plans.claim_plan(db_plans, EM)
    assert _snapshot(db_plans, plan_id) == before


def test_planning_costs_count_toward_the_day(db_plans: Conn) -> None:
    before = budgets.daily_spent(db_plans)
    _proposed(db_plans)
    assert budgets.daily_spent(db_plans) == before + Decimal("0.40")
    assert budgets.reserved(db_plans) == 0


def test_a_lease_can_be_renewed_only_by_its_holder(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    db_plans.execute("UPDATE agents SET max_concurrent = 1 WHERE key = 'EM-01'")
    assert plans.plan_heartbeat(db_plans, EM, plan_id, lease_seconds=600) is not None
    with pytest.raises(NotExecutionOwner):
        plans.plan_heartbeat(db_plans, DEV1, plan_id)


def _expire(db: Conn, plan_id: int) -> None:
    db.execute(
        "UPDATE plans SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (plan_id,)
    )


def test_an_expired_lease_returns_the_plan(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    _expire(db_plans, plan_id)
    assert plans.expire_plan_leases(db_plans) == [plan_id]
    plan = plans.get_plan(db_plans, plan_id)
    assert plan["status"] == "REQUESTED" and plan["attempt_count"] == 1
    assert _types(db_plans, plan_id)[-1] == "PLAN_TIMED_OUT"


def test_a_live_lease_is_left_alone(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    before = _snapshot(db_plans, plan_id)
    assert plans.expire_plan_leases(db_plans) == []
    assert _snapshot(db_plans, plan_id) == before


def test_a_late_submission_is_refused(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    _expire(db_plans, plan_id)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(LeaseExpired):
        plans.submit_plan(db_plans, EM, plan_id, GOOD)
    assert _snapshot(db_plans, plan_id) == before


# ---- proposing ------------------------------------------------------------------------------


def test_a_valid_plan_reaches_a_human(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    plan = plans.get_plan(db_plans, plan_id)
    assert plan["status"] == "PROPOSED" and plan["contract"]["summary"] == GOOD["summary"]
    assert plan["cost_usd"] == Decimal("0.40") and plan["attempt_count"] == 0
    assert list_tasks(db_plans) == []  # still nothing runs


def test_an_invalid_plan_never_reaches_a_human(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    assert plans.submit_plan(db_plans, EM, plan_id, BAD, {"cost": {"usd": "0.30"}}) == "REQUESTED"
    plan = plans.get_plan(db_plans, plan_id)
    assert plan["contract"] is None and plan["attempt_count"] == 1
    assert plan["cost_usd"] == Decimal("0.30")  # a refused plan still cost money
    assert ".github" in plan["feedback"][0]  # the next attempt is told what to fix
    next_attempt = plans.claim_plan(db_plans, EM)
    assert next_attempt is not None and ".github" in next_attempt.feedback[0]


@pytest.mark.parametrize(
    "shape", [{"summary": "s"}, {"summary": "s", "tasks": [], "approved": True}, {}]
)
def test_a_plan_off_the_contract_counts_as_a_failed_attempt(
    db_plans: Conn, shape: dict[str, Any]
) -> None:
    plan_id = _claimed(db_plans)
    assert plans.submit_plan(db_plans, EM, plan_id, shape) == "REQUESTED"
    assert "invalid plan" in plans.get_plan(db_plans, plan_id)["error"]


def test_three_invalid_plans_end_in_error(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    for expected in ("REQUESTED", "REQUESTED", "ERROR"):
        assert plans.claim_plan(db_plans, EM) is not None
        assert plans.submit_plan(db_plans, EM, plan_id, BAD) == expected
    assert plans.claim_plan(db_plans, EM) is None
    assert "ERROR" in build_report(db_plans) and "Plan" in build_report(db_plans)


def test_crashed_runs_retry_then_end_in_error(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    for i, expected in enumerate(("REQUESTED", "REQUESTED", "ERROR")):
        assert plans.claim_plan(db_plans, EM) is not None
        cost = {"cost": {"usd": "0.10"}}
        assert plans.fail_plan_run(db_plans, EM, plan_id, f"crash {i}", cost) == expected
    plan = plans.get_plan(db_plans, plan_id)
    assert plan["cost_usd"] == Decimal("0.30") and plan["error"] == "crash 2"


def test_only_the_holder_submits(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    before = _snapshot(db_plans, plan_id)
    for actor, error in ((DEV1, NotExecutionOwner), (HUMAN, PlanActorNotPermitted)):
        with pytest.raises(error):
            plans.submit_plan(db_plans, actor, plan_id, GOOD)
    assert _snapshot(db_plans, plan_id) == before


def test_a_plan_must_be_claimed_to_be_submitted(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(IllegalPlanTransition):
        plans.submit_plan(db_plans, EM, plan_id, GOOD)
    assert _snapshot(db_plans, plan_id) == before


# ---- deciding -------------------------------------------------------------------------------


def test_approving_creates_every_task_with_its_dependencies(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    plan = plans.approve_plan(db_plans, HUMAN, plan_id)
    assert plan["status"] == "APPROVED" and plan["decided_by"] == str(HUMAN)
    keys = plans.get_plan(db_plans, plan_id)["task_keys"]
    assert set(keys) == {"impl", "cli", "qa"}
    impl, cli, qa = (get_task(db_plans, keys[r]) for r in ("impl", "cli", "qa"))
    assert impl.state == cli.state == TaskState.READY
    assert qa.state == TaskState.PENDING and set(qa.dependencies) == {impl.key, cli.key}
    assert qa.assigned_role == "qa" and qa.allowed_paths == ["agent/tests/**"]
    assert impl.budget_usd == Decimal(2) and impl.priority == "P1" and impl.plan_id == plan_id
    assert _types(db_plans, plan_id)[-1] == "PLAN_APPROVED"
    assert check_invariants(db_plans) == []


def test_the_approving_human_creates_the_tasks(db_plans: Conn) -> None:
    keys = plans.get_plan(db_plans, plans.approve_plan(db_plans, HUMAN, _proposed(db_plans))["id"])
    created = db_plans.execute(
        "SELECT DISTINCT actor FROM events WHERE type = 'TASK_CREATED'"
    ).fetchall()
    assert keys["task_keys"] and [r["actor"] for r in created] == [str(HUMAN)]


def test_tasks_from_a_plan_run_under_the_usual_rules(db_plans: Conn) -> None:
    keys = plans.get_plan(db_plans, plans.approve_plan(db_plans, HUMAN, _proposed(db_plans))["id"])[
        "task_keys"
    ]
    do(db_plans, keys["impl"], TaskCommand.ASSIGN, agent_key="DEV-01")
    assert get_task(db_plans, keys["impl"]).run_budget_usd == Decimal(2)


def test_a_failure_midway_through_approval_creates_nothing(
    db_plans: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tasks are created, the third fails. Nothing may remain."""
    plan_id = _proposed(db_plans)
    before = _snapshot(db_plans, plan_id)
    task_seq = db_plans.execute("SELECT task_seq FROM projects WHERE key = 'AX'").fetchone()
    real, calls = plans.create_task, []

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["title"])
        if len(calls) == 3:
            raise RuntimeError("the disk filled up")
        return real(*args, **kwargs)

    monkeypatch.setattr(plans, "create_task", failing)
    with pytest.raises(RuntimeError):
        plans.approve_plan(db_plans, HUMAN, plan_id)
    assert len(calls) == 3
    assert _snapshot(db_plans, plan_id) == before
    assert list_tasks(db_plans) == []
    assert db_plans.execute("SELECT task_seq FROM projects WHERE key = 'AX'").fetchone() == task_seq
    assert check_invariants(db_plans) == []
    monkeypatch.setattr(plans, "create_task", real)
    assert plans.approve_plan(db_plans, HUMAN, plan_id)["status"] == "APPROVED"  # it can be retried


def test_approving_twice_creates_tasks_once(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    plans.approve_plan(db_plans, HUMAN, plan_id)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(IllegalPlanTransition):
        plans.approve_plan(db_plans, HUMAN, plan_id)
    assert _snapshot(db_plans, plan_id) == before


def test_two_approvals_at_once_create_tasks_once(db_plans: Conn, db_url: str) -> None:
    plan_id = _proposed(db_plans)
    barrier, results = threading.Barrier(2), []

    def approve() -> None:
        with connect(db_url) as conn:
            barrier.wait()
            try:
                plans.approve_plan(conn, HUMAN, plan_id)
                results.append("approved")
            except IllegalPlanTransition:
                results.append("refused")

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["approved", "refused"]
    assert len(list_tasks(db_plans)) == 3


@pytest.mark.parametrize("actor", [EM, DEV1, Actor(ActorKind.SYSTEM, "x")])
@pytest.mark.parametrize("decide", ["approve", "reject", "cancel"])
def test_only_a_human_can_decide(db_plans: Conn, actor: Actor, decide: str) -> None:
    plan_id = _proposed(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(PlanActorNotPermitted):
        if decide == "approve":
            plans.approve_plan(db_plans, actor, plan_id)
        elif decide == "reject":
            plans.reject_plan(db_plans, actor, plan_id, "no")
        else:
            plans.cancel_plan(db_plans, actor, plan_id)
    assert _snapshot(db_plans, plan_id) == before


def test_a_plan_must_be_proposed_to_be_approved(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(IllegalPlanTransition):
        plans.approve_plan(db_plans, HUMAN, plan_id)
    assert _snapshot(db_plans, plan_id) == before


def test_the_rules_run_again_at_approval(db_plans: Conn) -> None:
    """The project's test paths changed after the plan was proposed."""
    plan_id = _proposed(db_plans)
    set_test_paths(db_plans, HUMAN, "AX", [])
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(InvalidInput, match="no longer passes"):
        plans.approve_plan(db_plans, HUMAN, plan_id)
    assert _snapshot(db_plans, plan_id) == before


def test_rejecting_feeds_back_and_starts_the_attempts_over(db_plans: Conn) -> None:
    plan_id = _requested(db_plans)
    assert plans.claim_plan(db_plans, EM) is not None
    plans.fail_plan_run(db_plans, EM, plan_id, "crash")
    assert plans.claim_plan(db_plans, EM) is not None
    plans.submit_plan(db_plans, EM, plan_id, GOOD)
    plan = plans.reject_plan(db_plans, HUMAN, plan_id, "Split the CLI change out")
    assert plan["status"] == "REQUESTED" and plan["attempt_count"] == 0
    next_attempt = plans.claim_plan(db_plans, EM)
    assert next_attempt is not None
    assert "Split the CLI change out" in next_attempt.feedback[-1]
    assert next_attempt.previous_plan is not None  # it can see what it's changing
    assert list_tasks(db_plans) == []


def test_a_rejection_needs_feedback(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(InvalidInput):
        plans.reject_plan(db_plans, HUMAN, plan_id, "   ")
    assert _snapshot(db_plans, plan_id) == before


@pytest.mark.parametrize("setup", ["requested", "proposed"])
def test_cancelling(db_plans: Conn, setup: str) -> None:
    plan_id = _requested(db_plans) if setup == "requested" else _proposed(db_plans)
    plan = plans.cancel_plan(db_plans, HUMAN, plan_id, "not needed")
    assert plan["status"] == "CANCELLED" and plan["decided_by"] == str(HUMAN)
    assert plans.claim_plan(db_plans, EM) is None and list_tasks(db_plans) == []


def test_a_claimed_plan_cannot_be_cancelled_midway(db_plans: Conn) -> None:
    plan_id = _claimed(db_plans)
    before = _snapshot(db_plans, plan_id)
    with pytest.raises(IllegalPlanTransition):
        plans.cancel_plan(db_plans, HUMAN, plan_id)
    assert _snapshot(db_plans, plan_id) == before


def test_the_database_refuses_an_approval_without_a_human(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    with pytest.raises(Exception, match="plan_approval_has_a_human"), db_plans.transaction():
        db_plans.execute(
            "UPDATE plans SET status = 'APPROVED', decided_by = 'agent:EM-01' WHERE id = %s",
            (plan_id,),
        )


def test_the_invariants_notice_tasks_on_an_unapproved_plan(db_plans: Conn) -> None:
    plan_id = _proposed(db_plans)
    key = new_task(db_plans).key
    db_plans.execute("UPDATE tasks SET plan_id = %s WHERE key = %s", (plan_id, key))
    assert any("has 1 task" in v for v in check_invariants(db_plans))
    db_plans.execute("UPDATE tasks SET plan_id = NULL WHERE key = %s", (key,))


# ---- test paths -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["**", "/etc", "../x", "*", ".env", "a/../../b"])
def test_test_paths_must_be_real_paths(org: Conn, path: str) -> None:
    with pytest.raises(InvalidInput):
        set_test_paths(org, HUMAN, "AX", [path])


def test_agents_cannot_widen_the_test_paths(org: Conn) -> None:
    with pytest.raises(Forbidden):
        set_test_paths(org, EM, "AX", ["agent/**"])


# ---- over HTTP ------------------------------------------------------------------------------


@pytest.fixture
def client(api_tokens: Conn, db_url: str) -> Iterator[TestClient]:
    set_test_paths(api_tokens, HUMAN, "AX", ["agent/tests/**"])
    with TestClient(create_app(Settings(database_url=db_url), start_reaper=False)) as c:
        yield c


HUMAN_H = bearer("human:bharath")
EM_H = bearer("agent:EM-01")
DEV_H = bearer("agent:DEV-01")


def _through_http(c: TestClient) -> int:
    r = c.post(
        "/projects/AX/plans",
        json={"specification": "Build it.", "budget_usd": "10"},
        headers=HUMAN_H,
    )
    assert r.status_code == 201, r.text
    plan_id = int(r.json()["id"])
    claim = c.post("/plans/claim", json={}, headers=EM_H)
    assert claim.status_code == 200 and claim.json()["plan_id"] == plan_id
    assert c.post(f"/plans/{plan_id}/heartbeat", json={}, headers=EM_H).status_code == 200
    submit = c.post(f"/plans/{plan_id}/submit", json={"contract": GOOD}, headers=EM_H)
    assert submit.status_code == 200 and submit.json()["status"] == "PROPOSED"
    return plan_id


def test_the_whole_path_over_http(client: TestClient) -> None:
    plan_id = _through_http(client)
    assert client.post(f"/plans/{plan_id}/approve", headers=DEV_H).status_code == 403
    assert client.post(f"/plans/{plan_id}/approve", headers=EM_H).status_code == 403
    r = client.post(f"/plans/{plan_id}/approve", headers=HUMAN_H)
    assert r.status_code == 200 and set(r.json()["task_keys"]) == {"impl", "cli", "qa"}
    assert client.post(f"/plans/{plan_id}/approve", headers=HUMAN_H).status_code == 409
    listed = client.get("/plans?project=AX&status=APPROVED", headers=HUMAN_H).json()
    assert [p["id"] for p in listed] == [plan_id]
    assert "PLAN_APPROVED" in [
        e["type"] for e in client.get(f"/plans/{plan_id}/events", headers=HUMAN_H).json()
    ]


def test_routes_need_a_token(client: TestClient) -> None:
    assert client.get("/plans").status_code == 401
    assert client.post("/plans/claim").status_code == 401
    assert client.post("/plans/1/approve").status_code == 401


def test_nothing_to_claim_over_http(client: TestClient) -> None:
    assert client.post("/plans/claim", headers=EM_H).status_code == 204


def test_reject_and_cancel_over_http(client: TestClient) -> None:
    plan_id = _through_http(client)
    assert (
        client.post(f"/plans/{plan_id}/reject", json={"feedback": ""}, headers=HUMAN_H).status_code
        == 422
    )
    r = client.post(f"/plans/{plan_id}/reject", json={"feedback": "Smaller."}, headers=HUMAN_H)
    assert r.status_code == 200 and r.json()["status"] == "REQUESTED"
    r = client.post(f"/plans/{plan_id}/cancel", json={"reason": "no"}, headers=HUMAN_H)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"


def test_a_failed_run_over_http(client: TestClient) -> None:
    r = client.post(
        "/projects/AX/plans", json={"specification": "s", "budget_usd": "1"}, headers=HUMAN_H
    )
    plan_id = r.json()["id"]
    client.post("/plans/claim", headers=EM_H)
    r = client.post(
        f"/plans/{plan_id}/fail", json={"error": "crash", "cost": {"usd": "0.1"}}, headers=EM_H
    )
    assert r.status_code == 200 and r.json()["status"] == "REQUESTED"


def test_test_paths_over_http(client: TestClient) -> None:
    r = client.post("/projects/AX/test-paths", json={"test_paths": ["t/**"]}, headers=HUMAN_H)
    assert r.status_code == 200 and r.json()["test_paths"] == ["t/**"]
    assert (
        client.post(
            "/projects/AX/test-paths", json={"test_paths": ["**"]}, headers=HUMAN_H
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/projects/AX/test-paths", json={"test_paths": ["t/**"]}, headers=DEV_H
        ).status_code
        == 403
    )


# ---- dashboard and report -------------------------------------------------------------------


EVIL = '<script>alert("x")</script>'


def _login(c: TestClient) -> None:
    c.post("/ui/login", data={"token": TEST_TOKENS["human:bharath"]})


def test_a_proposed_plan_shows_under_needs_you(client: TestClient, api_tokens: Conn) -> None:
    plan_id = _through_http(client)
    _login(client)
    page = client.get("/ui").text
    assert "Needs you" in page and f"/ui/plans/{plan_id}" in page
    report = build_report(api_tokens)
    assert f"Plan {plan_id}" in report and "approve" in report


def test_the_plan_page_shows_what_a_decision_needs(client: TestClient) -> None:
    plan_id = _through_http(client)
    _login(client)
    page = client.get(f"/ui/plans/{plan_id}").text
    for wanted in (
        "Build it, then test it.",
        "agent/tests/**",
        "The CLI may change.",
        "/approve",
        "$6.00",
        "$10.00",
    ):
        assert wanted in page
    assert client.get("/ui/plans/999").status_code == 404
    client.post(f"/plans/{plan_id}/approve", headers=HUMAN_H)
    page = client.get(f"/ui/plans/{plan_id}").text
    assert "/ui/tasks/AX-" in page  # now it links to the real tasks


def test_a_plan_page_escapes_everything(client: TestClient) -> None:
    """A plan is a model's output, so every field is hostile."""
    r = client.post(
        "/projects/AX/plans", json={"specification": EVIL, "budget_usd": "9"}, headers=HUMAN_H
    )
    plan_id = r.json()["id"]
    client.post("/plans/claim", headers=EM_H)
    contract = {
        "summary": EVIL,
        "tasks": [
            _task(
                "a",
                title=EVIL,
                specification=EVIL,
                acceptance_criteria=[EVIL],
                allowed_paths=[f"agent/{EVIL}.py"],
            )
        ],
        "risks": [EVIL],
    }
    assert (
        client.post(f"/plans/{plan_id}/submit", json={"contract": contract}, headers=EM_H).json()[
            "status"
        ]
        == "PROPOSED"
    )
    client.post(f"/plans/{plan_id}/reject", json={"feedback": EVIL}, headers=HUMAN_H)
    _login(client)
    page = client.get(f"/ui/plans/{plan_id}").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page
