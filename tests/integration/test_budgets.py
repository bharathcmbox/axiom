"""Phase 2 T3: budgets are enforced (per task, per day), costs are recorded, contract fields."""

from decimal import Decimal

import pytest

from axiom.config import get_settings
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import AssignmentRejected, Forbidden, InvalidInput
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.tasks import get_task, set_task_budget
from axiom.registry.projects import create_project, get_project
from axiom.state.db import Conn
from tests.integration.helpers import DEV1, DEV2, HUMAN, do, events_for, new_task, snapshot

C, S = TaskCommand, TaskState


def _cost(usd: str, tokens_in: int = 1000, tokens_out: int = 200) -> dict[str, object]:
    return {"cost": {"usd": usd, "input_tokens": tokens_in, "output_tokens": tokens_out}}


def _run_and_fail(db: Conn, key: str, usd: str, agent: str = "DEV-01") -> None:
    actor = DEV1 if agent == "DEV-01" else DEV2
    do(db, key, C.ASSIGN, agent_key=agent)
    do(db, key, C.START, actor=actor)
    do(db, key, C.FAIL, actor=actor, payload={"error": "boom"} | _cost(usd))


@pytest.fixture
def daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal("2.00"))


def test_tasks_get_the_default_budget(org: Conn) -> None:
    assert new_task(org).budget_usd == Decimal("3.00")
    assert new_task(org, budget_usd=Decimal("7.50")).budget_usd == Decimal("7.50")


def test_submit_records_cost_tokens_branch_and_pr(org: Conn) -> None:
    key = new_task(org).key
    do(org, key, C.ASSIGN)
    do(org, key, C.START)
    payload = {
        "summary": "done",
        "branch": "axiom/AX-001-e1",
        "pr_url": "https://github.com/o/r/pull/7",
        "transcript_uri": "file:///work/AX-001-e1/transcript.jsonl",
    } | _cost("1.25", 50_000, 4_000)
    task = do(org, key, C.SUBMIT, payload=payload)
    assert (task.branch, task.pull_request_url) == ("axiom/AX-001-e1", payload["pr_url"])
    assert task.budget_spent_usd == Decimal("1.25")
    assert task.run_budget_usd == Decimal("1.75")
    row = org.execute(
        "SELECT cost_usd, input_tokens, output_tokens, transcript_uri FROM executions"
    ).fetchone()
    assert row == {
        "cost_usd": Decimal("1.2500"),
        "input_tokens": 50_000,
        "output_tokens": 4_000,
        "transcript_uri": payload["transcript_uri"],
    }


def test_failure_within_budget_retries_with_a_smaller_run_budget(org: Conn) -> None:
    key = new_task(org, budget_usd=Decimal("3.00")).key
    _run_and_fail(org, key, "1.00")
    task = get_task(org, key)
    assert (task.state, task.attempt_count, task.run_budget_usd) == (S.READY, 1, Decimal("2.00"))
    claimed = claim_next(org, DEV1)
    assert claimed is not None
    assert events_for(org, key)[-1]["payload"]["run_budget_usd"] == "2.00"


def test_exhausting_the_budget_fails_the_task_even_with_retries_left(org: Conn) -> None:
    key = new_task(org, budget_usd=Decimal("1.00"), max_retries=2).key
    _run_and_fail(org, key, "1.10")  # the estimate can overshoot the cap a little
    task = get_task(org, key)
    assert (task.state, task.attempt_count) == (S.FAILED, 1)
    assert events_for(org, key)[-1]["payload"]["budget_exhausted"] is True


def test_claim_refused_when_task_budget_is_spent(org: Conn) -> None:
    key = new_task(org, budget_usd=Decimal("1.00")).key
    _run_and_fail(org, key, "1.00")
    do(org, key, C.RETRY)  # human retries without raising the budget
    before = snapshot(org, key)
    with pytest.raises(AssignmentRejected, match="spent its budget"):
        claim_next(org, DEV1)
    assert snapshot(org, key) == before


def test_raising_the_budget_lets_work_continue(org: Conn) -> None:
    key = new_task(org, budget_usd=Decimal("1.00")).key
    _run_and_fail(org, key, "1.00")
    task = set_task_budget(org, HUMAN, key, Decimal("4.00"))
    assert task.run_budget_usd == Decimal("3.00")  # 4.00 budget - 1.00 spent
    do(org, key, C.RETRY)
    assert claim_next(org, DEV1) is not None
    assert "TASK_BUDGET_CHANGED" in [e["type"] for e in events_for(org, key)]


def test_daily_cap_refuses_new_work(org: Conn, daily_cap: None) -> None:
    first = new_task(org).key
    second = new_task(org).key
    _run_and_fail(org, first, "2.00")  # day is now spent (cap 2.00)
    before = snapshot(org, second)
    with pytest.raises(AssignmentRejected, match="daily budget"):
        do(org, second, C.ASSIGN, agent_key="DEV-02")
    assert snapshot(org, second) == before
    assert get_task(org, second).run_budget_usd == Decimal("0.00")


def test_run_budget_is_capped_by_the_day(org: Conn, daily_cap: None) -> None:
    key = new_task(org, budget_usd=Decimal("10.00")).key
    assert get_task(org, key).run_budget_usd == Decimal("2.00")


@pytest.mark.parametrize(
    "cost",
    [{"usd": -1}, {"usd": "lots"}, {"input_tokens": -5}, "1.00"],
)
def test_invalid_cost_is_rejected_without_changes(org: Conn, cost: object) -> None:
    key = new_task(org).key
    do(org, key, C.ASSIGN)
    do(org, key, C.START)
    before = snapshot(org, key)
    with pytest.raises(InvalidInput):
        do(org, key, C.SUBMIT, payload={"cost": cost})
    assert snapshot(org, key) == before


def test_only_humans_change_budgets(org: Conn) -> None:
    key = new_task(org).key
    with pytest.raises(Forbidden):
        set_task_budget(org, DEV1, key, Decimal("100"))
    with pytest.raises(InvalidInput):
        set_task_budget(org, HUMAN, key, Decimal("-1"))


def test_allowed_paths_are_stored_and_validated(org: Conn) -> None:
    paths = ["agent/pyproject.toml", "agent/uv.lock", "CLAUDE.md", "agent/tests/**"]
    assert new_task(org, allowed_paths=paths).allowed_paths == paths
    for bad in (["/etc/passwd"], ["../outside"], ["a/../../b"], [""], ["~/x"]):
        with pytest.raises(InvalidInput):
            new_task(org, allowed_paths=bad)


def test_project_repository_settings(org: Conn) -> None:
    create_project(
        org,
        HUMAN,
        key="SOC",
        name="tiny-agenticsoc",
        repo_url="https://github.com/bharathcmbox/tiny-agenticsoc.git",
        test_command="cd agent && uv run --with pytest pytest -q",
    )
    p = get_project(org, "SOC")
    assert (p.default_branch, p.test_command) == (
        "main",
        "cd agent && uv run --with pytest pytest -q",
    )
