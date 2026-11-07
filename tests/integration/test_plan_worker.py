"""Phase 4 T8: the planner worker end to end: real API over HTTP, real git, a fake planner."""

import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.modelproxy.client import Metered
from axiom.orchestrator import plans
from axiom.orchestrator.tasks import list_tasks
from axiom.registry.projects import set_test_paths
from axiom.runtimes.base import Outcome
from axiom.runtimes.fake_plan import FakePlanner, PlanScenario, good_plan
from axiom.runtimes.plan import PlanOutcome, PlanRequest
from axiom.runtimes.sandbox import MODEL_RUN_FILE
from axiom.state.db import Conn
from axiom.state.invariants import check_invariants
from axiom.worker.api_client import AxiomClient
from axiom.worker.plan_harness import REPORTED, PlanWorker, PlanWorkerConfig
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import HUMAN
from tests.integration.workers_shared import make_project

EM = Actor(ActorKind.AGENT, "EM-01")


def _worker(api_url: str, tmp_path: Path, runtime: Any, **kwargs: Any) -> PlanWorker:
    config = PlanWorkerConfig(
        agent_key="EM-01",
        workspaces_root=tmp_path / "plans",
        github_token=None,
        heartbeat_seconds=0.2,
    )
    client = AxiomClient(api_url, TEST_TOKENS["agent:EM-01"])
    return PlanWorker(config, client, lambda: runtime, **kwargs)


def _request(db: Conn, remote: Path) -> int:
    make_project(db, remote, test_command=None)
    set_test_paths(db, HUMAN, "SOC", ["tests/**"])
    return int(
        plans.request_plan(
            db, HUMAN, project_key="SOC", specification="Build it.", budget_usd=Decimal(10)
        )["id"]
    )


def test_a_good_plan_reaches_a_human_and_creates_nothing(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    planner = FakePlanner()
    assert _worker(api_url, tmp_path, planner).run_once()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "PROPOSED" and plan["contract"]["summary"]
    assert plan["cost_usd"] == Decimal("0.35")
    assert list_tasks(api_tokens) == []  # only approval creates tasks
    seen = planner.seen[0]
    assert seen.test_paths == ["tests/**"] and seen.plan_budget_usd == Decimal(10)
    assert seen.budget_usd == Decimal("1.00")


def test_it_plans_on_a_real_checkout_of_the_default_branch(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _request(api_tokens, remote)
    planner = FakePlanner()
    worker = _worker(api_url, tmp_path, planner)
    worker.config.keep_workspaces = True
    worker.run_once()
    workspace = planner.seen[0].workspace
    assert (workspace / "CLAUDE.md").read_text() == "# guide\n"
    assert (workspace.parent / "harness.git").is_dir()  # sealed against what it might plant


def test_it_runs_on_the_agents_model(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _request(api_tokens, remote)
    planner = FakePlanner()
    _worker(api_url, tmp_path, planner).run_once()
    assert planner.seen[0].model == "claude-opus-5-5"


def test_a_rule_breaking_plan_goes_back_with_its_problems(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    worker = _worker(api_url, tmp_path, FakePlanner(scenario=PlanScenario.BREAKS_THE_RULES))
    assert worker.run_once()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "REQUESTED" and plan["attempt_count"] == 1
    assert plan["cost_usd"] == Decimal("0.35") and ".github" in plan["feedback"][0]
    fixed = FakePlanner()
    assert _worker(api_url, tmp_path, fixed).run_once()
    assert ".github" in fixed.seen[0].feedback[0]  # the next run was told
    assert plans.get_plan(api_tokens, plan_id)["status"] == "PROPOSED"


def test_an_off_contract_plan_goes_back_with_its_problems(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner(scenario=PlanScenario.MALFORMED)).run_once()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "REQUESTED" and "invalid plan" in plan["feedback"][0]


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        (PlanScenario.CRASH, "planner crashed"),
        (PlanScenario.TRY_TO_EDIT, "planner modified the workspace"),
    ],
)
def test_bad_runs_count_as_failed_attempts(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path, scenario: PlanScenario, error: str
) -> None:
    plan_id = _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner(scenario=scenario)).run_once()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "REQUESTED" and plan["attempt_count"] == 1
    assert error in plan["error"] and plan["cost_usd"] == Decimal("0.35")


def test_a_plan_run_out_of_budget_isnt_retried(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner(scenario=PlanScenario.BUDGET_EXCEEDED)).run_once()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "ERROR" and "budget exceeded" in plan["error"]
    assert _worker(api_url, tmp_path, FakePlanner()).run_once() is False


def test_three_bad_runs_end_in_error(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    for _ in range(3):
        _worker(api_url, tmp_path, FakePlanner(scenario=PlanScenario.CRASH)).run_once()
    assert plans.get_plan(api_tokens, plan_id)["status"] == "ERROR"
    assert _worker(api_url, tmp_path, FakePlanner()).run_once() is False


def test_a_rejection_reaches_the_next_run_with_the_old_plan(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner()).run_once()
    plans.reject_plan(api_tokens, HUMAN, plan_id, "Split it up")
    next_run = FakePlanner()
    _worker(api_url, tmp_path, next_run).run_once()
    assert "Split it up" in next_run.seen[0].feedback[-1]
    assert next_run.seen[0].previous_plan is not None


def test_each_run_gets_its_own_workspace(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    a, b = FakePlanner(scenario=PlanScenario.CRASH), FakePlanner()
    _worker(api_url, tmp_path, a).run_once()
    _worker(api_url, tmp_path, b).run_once()
    assert a.seen[0].workspace != b.seen[0].workspace
    assert f"plan-{plan_id}-a2" in str(b.seen[0].workspace)


def test_the_workspace_is_removed_afterwards(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner()).run_once()
    assert list((tmp_path / "plans").iterdir()) == []


def test_a_project_without_a_repository_fails_before_spending(
    api_tokens: Conn, api_url: str, tmp_path: Path
) -> None:
    plan_id = int(
        plans.request_plan(
            api_tokens, HUMAN, project_key="AX", specification="s", budget_usd=Decimal(1)
        )["id"]
    )
    planner = FakePlanner()
    _worker(api_url, tmp_path, planner).run_once()
    assert planner.seen == []
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "REQUESTED" and "no repo_url" in plan["error"]


def test_a_credential_in_an_error_is_not_stored(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)

    class Leaky(FakePlanner):
        def run_plan(self, request: PlanRequest, on_event: Any = None) -> PlanOutcome:
            secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"
            return PlanOutcome(Outcome.FAILED, error=f"crash, key was {secret}")

    _worker(api_url, tmp_path, Leaky()).run_once()
    assert "sk-ant" not in plans.get_plan(api_tokens, plan_id)["error"]


class SpendingHang(FakePlanner):
    def run_plan(self, request: PlanRequest, on_event: Any = None) -> PlanOutcome:
        outcome = super().run_plan(request, on_event)
        line = {
            "type": "AssistantMessage",
            "data": {
                "message_id": "m",
                "model": "claude-opus-5-5",
                "usage": {"input_tokens": 100_000},
            },
        }
        (request.output_dir / "transcript.jsonl").write_text(json.dumps(line) + "\n")
        return outcome


def test_a_lost_lease_cancels_the_run_and_still_reports_its_cost(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    worker = _worker(api_url, tmp_path, SpendingHang(scenario=PlanScenario.HANG))
    t = threading.Thread(target=worker.run_once, daemon=True)
    t.start()
    for _ in range(100):
        if plans.get_plan(api_tokens, plan_id)["status"] == "CLAIMED":
            break
        time.sleep(0.05)
    api_tokens.execute(
        "UPDATE plans SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (plan_id,)
    )
    assert plans.expire_plan_leases(api_tokens) == [plan_id]
    t.join(timeout=10)
    assert not t.is_alive()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["status"] == "REQUESTED"  # money, not state
    assert plan["cost_usd"] == Decimal("0.4") and plan["cost_estimated"] is True


def _killed_run(tmp_path: Path, plan_id: int, attempt: int = 1) -> Path:
    run_dir = tmp_path / "plans" / f"plan-{plan_id}-a{attempt}"
    (run_dir / "out").mkdir(parents=True)
    line = {
        "type": "AssistantMessage",
        "data": {
            "message_id": "m",
            "model": "claude-opus-5-5",
            "usage": {"input_tokens": 100_000},
        },
    }
    (run_dir / "out" / "transcript.jsonl").write_text(json.dumps(line) + "\n")
    return run_dir


def test_a_restarted_planner_costs_the_run_it_lost_once(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    _killed_run(tmp_path, plan_id)
    worker = _worker(api_url, tmp_path, FakePlanner())
    assert worker.recover_costs() == 1
    assert worker.recover_costs() == 0  # a second restart changes nothing
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["cost_usd"] == Decimal("0.4") and plan["cost_estimated"] is True


def test_recovery_prefers_the_proxys_count(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    run_dir = _killed_run(tmp_path, plan_id)
    (run_dir / MODEL_RUN_FILE).write_text(json.dumps({"run_id": "mr_x"}))

    class Proxy:
        def close(self, run_id: str) -> Metered:
            return Metered(1, 0, Decimal("0.77"), 10, 10)

    worker = _worker(api_url, tmp_path, FakePlanner(), model_proxy=Proxy())
    worker.recover_costs()
    plan = plans.get_plan(api_tokens, plan_id)
    assert plan["cost_usd"] == Decimal("0.77") and plan["cost_estimated"] is False


def test_a_run_already_reported_isnt_counted_again(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    run_dir = _killed_run(tmp_path, plan_id)
    (run_dir / REPORTED).touch()
    assert _worker(api_url, tmp_path, FakePlanner()).recover_costs() == 0
    assert plans.get_plan(api_tokens, plan_id)["cost_usd"] is None


def test_only_planners_report_planning_costs(api_tokens: Conn, remote: Path) -> None:
    plan_id = _request(api_tokens, remote)
    count = api_tokens.execute("SELECT count(*) AS n FROM events").fetchone()
    from axiom.errors import Forbidden
    from tests.integration.helpers import DEV1

    for actor in (DEV1, HUMAN):
        with pytest.raises(Forbidden):
            plans.record_plan_cost(api_tokens, actor, plan_id, {"cost": {"usd": "1"}})
    assert api_tokens.execute("SELECT count(*) AS n FROM events").fetchone() == count


def test_the_end_to_end_path_to_tasks(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    plan_id = _request(api_tokens, remote)
    _worker(api_url, tmp_path, FakePlanner(contract=good_plan(1.5))).run_once()
    plans.approve_plan(api_tokens, HUMAN, plan_id)
    assert len(list_tasks(api_tokens)) == 2
    assert check_invariants(api_tokens) == []
