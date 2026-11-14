"""The reviewer worker: claim a review → workspace at the submitted commit → read-only review run
→ submit the review contract (or report a failed run). Deterministic harness code (ADR-008/009).

The reviewer never sees GitHub or Axiom credentials; this harness talks to both. A lost lease
means the review was timed out or superseded meanwhile, so nothing is reported.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.domain.roles import DEFAULT_MODEL, Role
from axiom.modelproxy.client import ModelProxyClient
from axiom.runtimes.base import Outcome
from axiom.runtimes.estimate import with_estimate
from axiom.runtimes.review import ReviewOutcome, ReviewRequest, ReviewRuntime
from axiom.worker.api_client import ApiError, AxiomClient
from axiom.worker.recover_costs import recover_costs, recovered_cost
from axiom.worker.verify import redact
from axiom.workspaces.git import git
from axiom.workspaces.manager import (
    ReviewWorkspace,
    prepare_review_workspace,
    remove_review_workspace,
)

log = logging.getLogger("axiom.reviewer")


@dataclass
class ReviewWorkerConfig:
    agent_key: str
    workspaces_root: Path
    github_token: str | None
    lease_seconds: int = 300
    heartbeat_seconds: float = 30.0
    poll_seconds: float = 10.0
    effort: str = "high"
    # Overrides the role's default (domain.roles.DEFAULT_MODEL), which ADR-009's check counts.
    default_model: str | None = None
    keep_workspaces: bool = False


@dataclass
class ReviewWorker:
    config: ReviewWorkerConfig
    api: AxiomClient
    runtime_factory: Callable[[], ReviewRuntime]
    model_proxy: ModelProxyClient | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    def run_forever(self) -> None:
        log.info("reviewer %s started", self.config.agent_key)
        try:
            recover_costs(self.api, self.config.workspaces_root, kinds="r", proxy=self.model_proxy)
        except Exception:
            log.exception("could not recover the costs of killed runs")
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("reviewer loop error")
                worked = False
            if not worked:
                self._stop.wait(self.config.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        try:
            assignment = self.api.claim_review(self.config.lease_seconds)
        except ApiError as e:
            if e.code == "assignment_rejected":
                log.info("cannot claim a review: %s", e)
                return False
            raise
        if assignment is None:
            return False
        self.process(assignment)
        return True

    def process(self, a: dict[str, Any]) -> None:
        review_id, task = a["review_id"], a["task"]
        runtime = self.runtime_factory()
        lost, hb_stop = threading.Event(), threading.Event()
        hb = threading.Thread(
            target=self._heartbeat, args=(review_id, hb_stop, lost, runtime), daemon=True
        )
        hb.start()
        ws: ReviewWorkspace | None = None
        try:
            project = self.api.project_by_id(task["project_id"])
            self._recover_earlier_attempt(review_id, task["key"])
            ws = prepare_review_workspace(
                self.config.workspaces_root,
                task_key=task["key"],
                review_id=review_id,
                repo_url=project["repo_url"],
                branch=task["branch"],
                head_sha=a["head_sha"],
                default_branch=project["default_branch"],
                token=self.config.github_token,
            )
            outcome = with_estimate(runtime.run_review(self._request(a, ws)), ws.out)
            if lost.is_set():
                log.warning("review %s: lease lost; reporting only what the run cost", review_id)
                if outcome.cost_usd is not None:
                    self._try_record_cost(review_id, outcome.cost_payload())
                return
            self._report(review_id, ws, outcome)
        except Exception as e:
            log.exception("review %s failed", review_id)
            if not lost.is_set():
                self._try_fail(review_id, f"reviewer worker error: {type(e).__name__}: {e}", {})
        finally:
            hb_stop.set()
            hb.join(timeout=10)
            if ws and not self.config.keep_workspaces:
                remove_review_workspace(ws)

    def _request(self, a: dict[str, Any], ws: ReviewWorkspace) -> ReviewRequest:
        task = a["task"]
        agent = self.api.agent(self.config.agent_key)
        return ReviewRequest(
            task_key=task["key"],
            review_id=a["review_id"],
            role=a["role"],
            title=task["title"],
            specification=task["specification"],
            acceptance_criteria=task["acceptance_criteria"],
            allowed_paths=task["allowed_paths"],
            diff=ws.diff,
            developer_result=a.get("developer_result"),
            workspace=ws.repo,
            output_dir=ws.out,
            budget_usd=Decimal(str(a["run_budget_usd"])),
            model=agent.get("model") or self.config.default_model or DEFAULT_MODEL[Role(a["role"])],
            effort=self.config.effort,
            previous_findings=self._previous_findings(task["key"], a["round"]),
        )

    def _previous_findings(self, key: str, round_: int) -> list[str]:
        """Findings from the previous round, so reviewers can check each one was fixed."""
        if round_ <= 1:
            return []
        out = []
        for r in self.api.task_reviews(key):
            if r["round"] == round_ - 1 and r["status"] == "FAIL" and r.get("contract"):
                for f in r["contract"].get("findings", []):
                    where = f"{f.get('file') or '?'}:{f.get('line') or '?'}"
                    out.append(f"[{f['severity']}] {where} {f['issue']} → {f['required_change']}")
        return out

    def _heartbeat(
        self, review_id: int, stop: threading.Event, lost: threading.Event, runtime: ReviewRuntime
    ) -> None:
        while not stop.wait(self.config.heartbeat_seconds):
            try:
                self.api.review_heartbeat(review_id, self.config.lease_seconds)
            except ApiError as e:
                if e.status in (403, 409, 422):  # expired, superseded or no longer ours
                    log.warning("review %s: heartbeat rejected (%s); cancelling", review_id, e.code)
                    lost.set()
                    runtime.cancel()
                    return
            except OSError as e:
                log.warning("review %s: heartbeat error: %s", review_id, e)

    def _report(self, review_id: int, ws: ReviewWorkspace, outcome: ReviewOutcome) -> None:
        cost = outcome.cost_payload()
        if outcome.outcome == Outcome.COMPLETED and outcome.contract is not None:
            # Defence in depth: a read-only reviewer must leave the checkout untouched.
            dirty = git(
                ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
                cwd=ws.repo,
            )
            if dirty:
                self._try_fail(review_id, f"reviewer modified the workspace: {dirty[:300]}", cost)
                return
            decision = self.api.submit_review(
                review_id, outcome.contract.model_dump(mode="json"), cost
            )
            log.info("review %s: %s -> %s", review_id, outcome.contract.verdict, decision)
            return
        self._try_fail(
            review_id,
            outcome.error or outcome.outcome.value,
            cost,
            final=outcome.outcome == Outcome.BUDGET_EXCEEDED,
        )

    def _recover_earlier_attempt(self, review_id: int, task_key: str) -> None:
        """A killed attempt left its transcript where this attempt is about to work."""
        cost = recovered_cost(
            self.config.workspaces_root / f"{task_key}-r{review_id}", self.model_proxy
        )
        if cost is not None:
            self._try_record_cost(review_id, cost)

    def _try_record_cost(self, review_id: int, cost: dict[str, Any]) -> None:
        try:
            self.api.record_review_cost(review_id, cost)
        except (ApiError, OSError) as e:
            log.warning("review %s: could not record its cost: %s", review_id, e)

    def _try_fail(
        self, review_id: int, error: str, cost: dict[str, Any], *, final: bool = False
    ) -> None:
        try:
            self.api.fail_review(review_id, redact(error)[:2000], cost, final=final)
        except ApiError as e:
            log.warning("review %s: could not report failure (%s)", review_id, e)
