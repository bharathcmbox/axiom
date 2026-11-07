"""The planner worker's harness: claim a plan, run the planner in a sandbox, report (ADR-011).

Holds the GitHub and agent tokens; the planner never sees them. The API alone judges a proposal. A
marker beside the workspace says its cost was reported, so a restart counts a killed run once.
"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.modelproxy.client import ModelProxyClient, ModelProxyError
from axiom.runtimes.base import Outcome
from axiom.runtimes.estimate import estimate_from_transcript, with_estimate
from axiom.runtimes.plan import PlanOutcome, PlanRequest, PlanRuntime
from axiom.runtimes.sandbox import MODEL_RUN_FILE
from axiom.worker.api_client import ApiError, AxiomClient
from axiom.worker.verify import redact
from axiom.workspaces.git import git
from axiom.workspaces.manager import PlanWorkspace, prepare_plan_workspace, remove_plan_workspace

log = logging.getLogger("axiom.planner")
REPORTED = "cost-reported"


@dataclass
class PlanWorkerConfig:
    agent_key: str
    workspaces_root: Path
    github_token: str | None
    lease_seconds: int = 300
    heartbeat_seconds: float = 30.0
    poll_seconds: float = 10.0
    effort: str = "high"
    default_model: str = "claude-opus-5-5"
    keep_workspaces: bool = False


@dataclass
class PlanWorker:
    config: PlanWorkerConfig
    api: AxiomClient
    runtime_factory: Callable[[], PlanRuntime]
    model_proxy: ModelProxyClient | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def run_forever(self) -> None:
        log.info("planner %s started", self.config.agent_key)
        self.recover_costs()
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("planner loop error")
                worked = False
            if not worked:
                self._stop.wait(self.config.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        try:
            assignment = self.api.claim_plan(self.config.lease_seconds)
        except ApiError as e:
            if e.code == "assignment_rejected":
                log.info("cannot claim a plan: %s", e)
                return False
            raise
        if assignment is None:
            return False
        self.process(assignment)
        return True

    def process(self, a: dict[str, Any]) -> None:
        plan_id = a["plan_id"]
        runtime = self.runtime_factory()
        lost, hb_stop = threading.Event(), threading.Event()
        hb = threading.Thread(
            target=self._heartbeat, args=(plan_id, hb_stop, lost, runtime), daemon=True
        )
        hb.start()
        ws: PlanWorkspace | None = None
        try:
            if not a.get("repo_url"):
                raise ValueError(f"project {a['project_key']} has no repo_url")
            ws = prepare_plan_workspace(
                self.config.workspaces_root,
                plan_id=plan_id,
                run_number=a["run_number"],
                repo_url=a["repo_url"],
                default_branch=a["default_branch"],
                token=self.config.github_token,
            )
            outcome = with_estimate(runtime.run_plan(self._request(a, ws)), ws.out)
            if lost.is_set():
                log.warning("plan %s: lease lost; reporting only what the run cost", plan_id)
                self._record_cost(plan_id, ws.root, outcome.cost_payload())
                return
            self._report(plan_id, ws, outcome)
        except Exception as e:
            log.exception("plan %s failed", plan_id)
            if not lost.is_set():
                self._try_fail(plan_id, f"planner worker error: {type(e).__name__}: {e}", {})
        finally:
            hb_stop.set()
            hb.join(timeout=10)
            if ws and not self.config.keep_workspaces:
                remove_plan_workspace(ws)

    def _request(self, a: dict[str, Any], ws: PlanWorkspace) -> PlanRequest:
        agent = self.api.agent(self.config.agent_key)
        return PlanRequest(
            plan_id=a["plan_id"],
            project_key=a["project_key"],
            specification=a["specification"],
            plan_budget_usd=Decimal(str(a["budget_usd"])),
            test_paths=a["test_paths"],
            workspace=ws.repo,
            output_dir=ws.out,
            budget_usd=Decimal(str(a["run_budget_usd"])),
            feedback=a["feedback"],
            previous_plan=a["previous_plan"],
            model=agent.get("model") or self.config.default_model,
            effort=self.config.effort,
        )

    def _report(self, plan_id: int, ws: PlanWorkspace, outcome: PlanOutcome) -> None:
        cost = outcome.cost_payload()
        (ws.root / REPORTED).touch()
        if outcome.outcome == Outcome.COMPLETED and outcome.contract is not None:
            # Defence in depth: a read-only planner must leave the checkout untouched.
            dirty = git(
                ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
                cwd=ws.repo,
            )
            if dirty:
                self._try_fail(plan_id, f"planner modified the workspace: {dirty[:300]}", cost)
                return
            status = self.api.submit_plan(plan_id, outcome.contract, cost)
            log.info("plan %s: %s", plan_id, status)
            return
        self._try_fail(plan_id, outcome.error or outcome.outcome.value, cost)

    def _try_fail(self, plan_id: int, error: str, cost: dict[str, Any]) -> None:
        try:
            self.api.fail_plan(plan_id, redact(error)[:2000], cost)
        except (ApiError, OSError) as e:
            log.warning("plan %s: could not report failure (%s)", plan_id, e)

    def _record_cost(self, plan_id: int, root: Path, cost: dict[str, Any]) -> None:
        if cost.get("usd") is None:
            return
        try:
            self.api.record_plan_cost(plan_id, cost)
            (root / REPORTED).touch()
        except (ApiError, OSError) as e:
            log.warning("plan %s: could not record its cost: %s", plan_id, e)

    def _heartbeat(
        self, plan_id: int, stop: threading.Event, lost: threading.Event, runtime: PlanRuntime
    ) -> None:
        while not stop.wait(self.config.heartbeat_seconds):
            try:
                self.api.plan_heartbeat(plan_id, self.config.lease_seconds)
            except ApiError as e:
                if e.status in (403, 409, 422):  # expired, finished or no longer ours
                    log.warning("plan %s: heartbeat rejected (%s); cancelling", plan_id, e.code)
                    lost.set()
                    runtime.cancel()
                    return
            except OSError as e:
                log.warning("plan %s: heartbeat error: %s", plan_id, e)

    def recover_costs(self) -> int:
        """A killed worker reported nothing. Its workspace, and the proxy, know what it spent."""
        root = self.config.workspaces_root
        recorded = 0
        for run_dir in sorted(root.glob("plan-*-a*")) if root.is_dir() else []:
            if (run_dir / REPORTED).exists():
                continue
            try:
                plan_id = int(run_dir.name.split("-")[1])
                cost = self._cost_of(run_dir)
                if cost is not None:
                    self._record_cost(plan_id, run_dir, cost)
                    recorded += 1
            except (ValueError, IndexError) as e:
                log.info("no cost recovered for %s: %s", run_dir.name, e)
        return recorded

    def _cost_of(self, run_dir: Path) -> dict[str, Any] | None:
        run_file = run_dir / MODEL_RUN_FILE
        if self.model_proxy is not None and run_file.is_file():
            try:
                metered = self.model_proxy.close(json.loads(run_file.read_text())["run_id"])
                return metered.cost_payload()
            except ModelProxyError, ValueError, KeyError:
                pass
        found = estimate_from_transcript(run_dir / "out" / "transcript.jsonl")
        if found is None:
            return None
        return {
            "usd": str(found.usd),
            "input_tokens": found.input_tokens,
            "output_tokens": found.output_tokens,
            "estimated": True,
        }
