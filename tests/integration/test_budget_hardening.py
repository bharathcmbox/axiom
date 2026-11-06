"""Phase 4 T3: run budgets are reserved at claim time; reviews respect the task's budget; costs of
runs that never reported are still recorded; a project chooses its Python."""

import contextlib
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from axiom.api.app import create_app
from axiom.config import Settings, get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import AssignmentRejected, Forbidden, InvalidInput, NotExecutionOwner
from axiom.orchestrator import budgets
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.commands import record_execution_cost
from axiom.orchestrator.reviews import (
    claim_review,
    fail_review_run,
    list_reviews,
    record_review_cost,
    submit_review,
)
from axiom.orchestrator.tasks import get_task, set_task_budget
from axiom.registry.projects import get_project, set_python_version
from axiom.runtimes.base import RunRequest
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.state.db import Conn, connect
from axiom.worker.harness import test_command as command_for
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import DEV1, DEV2, HUMAN, do, drive_to, events_for, new_task
from tests.integration.test_reviews import PASS, REV, SEC, _in_review

C, S = TaskCommand, TaskState
ESTIMATED = {"cost": {"usd": "0.14", "input_tokens": 9, "output_tokens": 3, "estimated": True}}


def _executions(db: Conn, key: str) -> list[dict[str, Any]]:
    return db.execute(
        """SELECT x.* FROM executions x JOIN tasks t ON t.id = x.task_id
           WHERE t.key = %s ORDER BY x.id""",
        (key,),
    ).fetchall()


def _state_of(db: Conn, key: str) -> tuple[Any, ...]:
    return (get_task(db, key).state, len(events_for(db, key)), len(_executions(db, key)))


@pytest.fixture
def small_day(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    monkeypatch.setenv("AXIOM_DAILY_BUDGET_USD", "7.00")
    yield
    get_settings.cache_clear()


# ---- reservations --------------------------------------------------------------------------


def test_a_run_reserves_its_budget_and_a_second_gets_the_rest(org: Conn, small_day: None) -> None:
    first, second = (
        new_task(org, budget_usd=Decimal(5)).key,
        new_task(org, budget_usd=Decimal(5)).key,
    )
    do(org, first, C.ASSIGN, agent_key="DEV-01")
    do(org, second, C.ASSIGN, agent_key="DEV-02")
    assert get_task(org, first).run_budget_usd == Decimal(5)  # its own reservation, not what's left
    assert get_task(org, second).run_budget_usd == Decimal(2)  # 7 - 5 reserved
    assert _executions(org, second)[0]["run_budget_usd"] == Decimal(2)
    assert budgets.reserved(org) == Decimal(7)


def test_a_claim_is_refused_when_the_rest_is_reserved(
    org: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal(6))
    first, second = new_task(org, budget_usd=Decimal(6)).key, new_task(org).key
    do(org, first, C.ASSIGN, agent_key="DEV-01")
    before = _state_of(org, second)
    with pytest.raises(AssignmentRejected, match="reserved by runs in flight"):
        do(org, second, C.ASSIGN, agent_key="DEV-02")
    assert _state_of(org, second) == before


def test_a_finished_run_gives_back_what_it_did_not_spend(org: Conn, small_day: None) -> None:
    first, second = new_task(org, budget_usd=Decimal(7)).key, new_task(org).key
    do(org, first, C.ASSIGN, agent_key="DEV-01")
    do(org, first, C.START, DEV1)
    do(org, first, C.SUBMIT, DEV1, payload={"summary": "ok", "cost": {"usd": "1.00"}})
    assert budgets.reserved(org) == 0
    do(org, second, C.ASSIGN, agent_key="DEV-02")
    assert get_task(org, second).run_budget_usd == Decimal(3)  # the task default; 6 are free


def test_one_run_cannot_reserve_the_whole_day(org: Conn) -> None:
    key = new_task(org, budget_usd=Decimal(50)).key
    do(org, key, C.ASSIGN)
    assert get_task(org, key).run_budget_usd == get_settings().max_run_budget_usd


def _claim(db_url: str, barrier: threading.Barrier, actor: Actor) -> None:
    with connect(db_url) as conn:
        barrier.wait()
        claim_next(conn, actor)


def test_racing_claims_never_reserve_more_than_the_day(org: Conn, db_url: str) -> None:
    """Both developers claim at once with $4 tasks against a $7 day. Their claims are also
    serialized by the developer cap: the race the budget lock alone must win is the reviewers'."""
    get_settings.cache_clear()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("AXIOM_DAILY_BUDGET_USD", "7.00")
    try:
        for _ in range(8):
            keys = [new_task(org, budget_usd=Decimal(4)).key for _ in range(2)]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(target=_claim, args=(db_url, barrier, actor))
                for actor in (DEV1, DEV2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert budgets.reserved(org) == Decimal(7)  # 4 + the 3 left, never 4 + 4
            for key in keys:
                do(org, key, C.CANCEL, HUMAN)
    finally:
        monkey.undo()
        get_settings.cache_clear()


def _claim_review_racing(db_url: str, barrier: threading.Barrier, actor: Actor) -> None:
    with connect(db_url) as conn:
        barrier.wait()
        with contextlib.suppress(AssignmentRejected):  # the loser, when nothing is left for it
            claim_review(conn, actor)


def test_racing_reviewers_never_reserve_more_than_the_day(
    org: Conn, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewers take no developer-cap lock and these hold different tasks, so only the budget
    lock serializes them. With it removed, this fails: both see $1.50 free and take $1 each."""
    settings = get_settings()
    for _ in range(12):
        keys = [_in_review(org), _in_review(org)]
        monkeypatch.setattr(
            settings, "daily_budget_usd", budgets.daily_spent(org) + Decimal("1.50")
        )
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=_claim_review_racing, args=(db_url, barrier, actor))
            for actor in (REV, SEC)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert budgets.reserved(org) == Decimal("1.50")  # $1 + the 50c left, never $1 + $1
        for key in keys:
            do(org, key, C.CANCEL, HUMAN)


# ---- reviews and the task's budget ---------------------------------------------------------


def test_a_review_run_is_capped_by_what_the_task_has_left(org: Conn) -> None:
    key = _in_review(org)
    set_task_budget(
        org, HUMAN, key, budgets.task_spent(org, get_task(org, key).id) + Decimal("0.30")
    )
    claimed = claim_review(org, REV)
    assert claimed is not None and claimed.run_budget_usd == Decimal("0.30")
    assert list_reviews(org, get_task(org, key).id)[0]["run_budget_usd"] == Decimal("0.30")


def test_second_reviewer_waits_when_the_first_reserved_the_rest(org: Conn) -> None:
    key = _in_review(org)
    set_task_budget(
        org, HUMAN, key, budgets.task_spent(org, get_task(org, key).id) + Decimal("0.30")
    )
    assert claim_review(org, REV) is not None
    before = _state_of(org, key)
    with pytest.raises(AssignmentRejected, match="reserved by runs in flight"):
        claim_review(org, SEC)
    assert _state_of(org, key) == before
    assert get_task(org, key).state == S.IN_REVIEW  # waiting, not escalated: money may come back


def test_a_spent_task_escalates_instead_of_starting_a_review(org: Conn) -> None:
    key = _in_review(org)
    task = get_task(org, key)
    claimed = claim_review(org, REV)
    assert claimed is not None
    submit_review(org, REV, claimed.review_id, PASS, {"cost": {"usd": "0.50"}})
    set_task_budget(org, HUMAN, key, Decimal("0.50"))
    assert claim_review(org, SEC) is None
    assert get_task(org, key).state == S.WAITING_FOR_HUMAN
    event = [e for e in events_for(org, key) if e["to_state"] == S.WAITING_FOR_HUMAN][-1]
    assert "budget" in event["payload"]["reason"] and event["actor"] == "system:aggregator"
    assert {r["status"] for r in list_reviews(org, task.id)} == {"PASS", "SUPERSEDED"}


def test_a_failed_attempts_cost_survives_the_retry(org: Conn) -> None:
    """Found while writing T3: submitting used to overwrite the cost of earlier attempts."""
    key = _in_review(org)
    first = claim_review(org, REV)
    assert first is not None
    fail_review_run(org, REV, first.review_id, "crashed", {"cost": {"usd": "0.20"}})
    second = claim_review(org, REV)
    assert second is not None and second.review_id == first.review_id
    submit_review(org, REV, second.review_id, PASS, {"cost": {"usd": "0.30"}})
    review = next(r for r in list_reviews(org, get_task(org, key).id) if r["id"] == first.review_id)
    assert review["cost_usd"] == Decimal("0.50")


# ---- costs of runs that never reported -----------------------------------------------------


def _timed_out_execution(db: Conn) -> tuple[str, int]:
    key = drive_to(db, S.IN_PROGRESS)
    db.execute("UPDATE executions SET lease_expires_at = now() - interval '1 second'")
    from axiom.orchestrator.leases import expire_leases

    expire_leases(db)
    return key, _executions(db, key)[-1]["id"]


def test_a_killed_runs_cost_is_recorded_and_marked(org: Conn) -> None:
    key, execution_id = _timed_out_execution(org)
    spent = budgets.task_spent(org, get_task(org, key).id)
    assert record_execution_cost(org, DEV1, execution_id, ESTIMATED) is True
    row = _executions(org, key)[-1]
    assert row["cost_usd"] == Decimal("0.14") and row["cost_estimated"] is True
    assert budgets.task_spent(org, get_task(org, key).id) == spent + Decimal("0.14")
    event = events_for(org, key)[-1]
    assert event["type"] == "EXECUTION_COST_RECORDED" and event["payload"]["estimated"] is True


def test_a_repeated_cost_report_cannot_double_count(org: Conn) -> None:
    key, execution_id = _timed_out_execution(org)
    record_execution_cost(org, DEV1, execution_id, ESTIMATED)
    count = len(events_for(org, key))
    assert record_execution_cost(org, DEV1, execution_id, {"cost": {"usd": "9.00"}}) is False
    assert _executions(org, key)[-1]["cost_usd"] == Decimal("0.14")
    assert len(events_for(org, key)) == count


def test_cost_cannot_be_recorded_on_a_running_execution(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    execution_id = _executions(org, key)[-1]["id"]
    before = _state_of(org, key)
    with pytest.raises(InvalidInput, match="still"):
        record_execution_cost(org, DEV1, execution_id, ESTIMATED)
    assert _state_of(org, key) == before and _executions(org, key)[-1]["cost_usd"] is None


def test_only_the_runs_agent_reports_its_cost(org: Conn) -> None:
    key, execution_id = _timed_out_execution(org)
    before = _state_of(org, key)
    for actor in (DEV2, HUMAN, Actor(ActorKind.SYSTEM, "reaper")):
        with pytest.raises(NotExecutionOwner):
            record_execution_cost(org, actor, execution_id, ESTIMATED)
    assert _state_of(org, key) == before and _executions(org, key)[-1]["cost_usd"] is None


def test_cost_needs_an_amount(org: Conn) -> None:
    _key, execution_id = _timed_out_execution(org)
    with pytest.raises(InvalidInput, match="required"):
        record_execution_cost(org, DEV1, execution_id, {"cost": {"input_tokens": 3}})


def test_a_killed_reviews_cost_is_recorded_once_by_any_reviewer(org: Conn) -> None:
    key = _in_review(org)
    claimed = claim_review(org, REV)
    assert claimed is not None
    assert record_review_cost(org, SEC, claimed.review_id, ESTIMATED) is True
    assert record_review_cost(org, REV, claimed.review_id, {"cost": {"usd": "9"}}) is False
    review = next(
        r for r in list_reviews(org, get_task(org, key).id) if r["id"] == claimed.review_id
    )
    assert review["cost_usd"] == Decimal("0.14") and review["cost_estimated"] is True
    assert review["status"] == "CLAIMED"  # money, not state


def test_only_reviewers_report_review_costs(org: Conn) -> None:
    key = _in_review(org)
    claimed = claim_review(org, REV)
    assert claimed is not None
    count = len(events_for(org, key))
    for actor in (DEV1, HUMAN):
        with pytest.raises(Forbidden):
            record_review_cost(org, actor, claimed.review_id, ESTIMATED)
    assert len(events_for(org, key)) == count


def test_the_dashboard_shows_an_estimate_as_an_estimate(api_tokens: Conn, db_url: str) -> None:
    key, execution_id = _timed_out_execution(api_tokens)
    record_execution_cost(api_tokens, DEV1, execution_id, ESTIMATED)
    with TestClient(create_app(Settings(database_url=db_url), start_reaper=False)) as c:
        c.post("/ui/login", data={"token": TEST_TOKENS["human:bharath"]})
        assert "~$0.14 est." in c.get(f"/ui/tasks/{key}").text


def test_late_cost_over_http(api_tokens: Conn, db_url: str) -> None:
    _key, execution_id = _timed_out_execution(api_tokens)
    headers = {"Authorization": f"Bearer {TEST_TOKENS['agent:DEV-01']}"}
    with TestClient(create_app(Settings(database_url=db_url), start_reaper=False)) as c:
        url = f"/executions/{execution_id}/cost"
        r = c.post(url, json={"cost": {"usd": "0.14", "estimated": True}}, headers=headers)
        assert r.status_code == 200 and r.json() == {"recorded": True}
        assert c.post(url, json={"cost": {"usd": "9"}}, headers=headers).json() == {
            "recorded": False
        }
        human = {"Authorization": f"Bearer {TEST_TOKENS['human:bharath']}"}
        assert c.post(url, json={"cost": {"usd": "9"}}, headers=human).status_code == 403


# ---- per-project Python --------------------------------------------------------------------


def test_a_project_chooses_its_python(org: Conn) -> None:
    project = set_python_version(org, HUMAN, "AX", "3.12")
    assert project.python_version == "3.12"
    assert get_project(org, "AX").python_version == "3.12"
    event = org.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert event is not None and event["type"] == "PROJECT_PYTHON_CHANGED"
    assert set_python_version(org, HUMAN, "AX", None).python_version is None


@pytest.mark.parametrize("version", ["2.7", "3.12; curl evil", "", "3.12 "])
def test_only_installed_pythons_are_accepted(org: Conn, version: str) -> None:
    count = org.execute("SELECT count(*) AS n FROM events").fetchone()
    with pytest.raises(InvalidInput, match="python_version"):
        set_python_version(org, HUMAN, "AX", version)
    assert get_project(org, "AX").python_version is None
    assert org.execute("SELECT count(*) AS n FROM events").fetchone() == count


def test_agents_cannot_choose_the_python(org: Conn) -> None:
    with pytest.raises(Forbidden):
        set_python_version(org, DEV1, "AX", "3.12")
    assert get_project(org, "AX").python_version is None


def test_the_database_refuses_a_malformed_python(org: Conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), org.transaction():
        org.execute("UPDATE projects SET python_version = '3.12; rm -rf /'")


def test_verified_tests_use_the_projects_python() -> None:
    command = "cd agent && uv run pytest"
    assert command_for({"test_command": command}) == command
    assert command_for({"test_command": command, "python_version": "3.12"}) == (
        "export UV_PYTHON=3.12; cd agent && uv run pytest"  # applies past the `cd`
    )


def test_the_agents_sandbox_gets_the_projects_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_container(self: SandboxRuntime, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"outcome": "failed", "error": "stop here"}

    monkeypatch.setattr(SandboxRuntime, "_run_container", fake_container)
    request = RunRequest(
        task_key="AX-1",
        title="t",
        specification="s",
        acceptance_criteria=[],
        allowed_paths=[],
        workspace=tmp_path,
        output_dir=tmp_path,
        budget_usd=Decimal(1),
        python_version="3.12",
    )
    SandboxRuntime().run(request)
    assert "UV_PYTHON=3.12" in seen["extra_env"]
