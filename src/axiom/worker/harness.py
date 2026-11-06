"""The worker harness: claim → start → heartbeat → workspace → agent → verify → publish → report.

Deterministic code only (ADR-002, ADR-008). The agent's output is treated as untrusted: it is
verified before anything is pushed, and the worker, not the agent, reports to Axiom.

If the lease is lost mid-run (heartbeat rejected), the run is cancelled and nothing is
reported: the reaper has already timed the execution out and the task is someone else's.
"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.modelproxy.client import ModelProxyClient
from axiom.runtimes.base import CommandRunner, Outcome, RunOutcome, RunRequest, Runtime
from axiom.runtimes.estimate import with_estimate
from axiom.worker.api_client import ApiError, AxiomClient
from axiom.worker.feedback import feedback_from_events
from axiom.worker.publish import PullRequest, pr_body, push_branch
from axiom.worker.recover_costs import recover_costs
from axiom.worker.verify import check_changes, commit, credentials_in, run_tests
from axiom.workspaces.manager import Workspace, prepare_workspace, remove_workspace

log = logging.getLogger("axiom.worker")

# (repo_url, branch, base, title, body) -> PullRequest
PrOpener = Callable[[str, str, str, str, str], PullRequest]
# (repo_url, pr_number, body) -> None
PrCommenter = Callable[[str, int, str], None]


@dataclass
class WorkerConfig:
    agent_key: str
    workspaces_root: Path
    github_token: str | None
    lease_seconds: int = 120
    heartbeat_seconds: float = 30.0
    poll_seconds: float = 10.0
    project_key: str | None = None
    effort: str = "high"
    default_model: str = "claude-opus-5-5"  # ADR-006; used when the agent row has no model
    test_timeout_seconds: int = 900
    keep_workspaces: bool = False


def test_command(project: dict[str, Any]) -> str:
    """The project's test command, with its Python chosen for uv. The version was validated on
    the way in, by the API and by a database constraint."""
    version = project.get("python_version")
    command = str(project["test_command"])
    return f"export UV_PYTHON={version}; {command}" if version else command


@dataclass
class Worker:
    config: WorkerConfig
    api: AxiomClient
    runtime_factory: Callable[[], Runtime]
    test_runner: CommandRunner
    open_pr: PrOpener
    comment_pr: PrCommenter = lambda repo_url, number, body: None
    # Checked before any work starts, so a misconfigured project fails fast instead of
    # spending an agent run and only failing at the PR step. Production: parse_github_repo.
    validate_repo: Callable[[str], object] = lambda url: None
    model_proxy: ModelProxyClient | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    # ---- loop ----------------------------------------------------------------------------

    def run_forever(self) -> None:
        log.info("worker %s started", self.config.agent_key)
        try:
            recover_costs(self.api, self.config.workspaces_root, kinds="e", proxy=self.model_proxy)
        except Exception:
            log.exception("could not recover the costs of killed runs")
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                self._stop.wait(self.config.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """Claim and process one task. Returns False if there was nothing to do."""
        try:
            task = self.api.claim(self.config.lease_seconds, self.config.project_key)
        except ApiError as e:
            if e.code == "assignment_rejected":  # at capacity, or budget spent
                log.info("cannot claim: %s", e)
                return False
            raise
        if task is None:
            return False
        self.process(task)
        return True

    # ---- one execution -------------------------------------------------------------------

    def process(self, task: dict[str, Any]) -> None:
        key = task["key"]
        execution = next(e for e in reversed(self.api.executions(key)) if e["status"] == "STARTING")
        self.api.command(key, "start", lease_seconds=self.config.lease_seconds)
        runtime = self.runtime_factory()
        lease_lost = threading.Event()
        hb_stop = threading.Event()
        hb = threading.Thread(
            target=self._heartbeat, args=(key, hb_stop, lease_lost, runtime), daemon=True
        )
        hb.start()
        workspace: Workspace | None = None
        try:
            project = self.api.project_by_id(task["project_id"])
            if not project.get("repo_url"):
                raise ValueError(f"project {project['key']} has no repo_url")
            self.validate_repo(project["repo_url"])
            workspace = prepare_workspace(
                self.config.workspaces_root,
                task_key=key,
                execution_id=execution["id"],
                repo_url=project["repo_url"],
                default_branch=project["default_branch"],
                token=self.config.github_token,
                existing_branch=task.get("branch"),  # rework continues the same PR
            )
            outcome = runtime.run(self._request(task, workspace, project))
            outcome = with_estimate(outcome, workspace.out)
            if lease_lost.is_set():
                log.warning("%s: lease lost during the run; reporting only its cost", key)
                self._try_record_cost(execution["id"], outcome)
                return
            self._report(task, project, execution["id"], workspace, outcome)
        except Exception as e:
            log.exception("%s: execution failed", key)
            if not lease_lost.is_set():
                self._try_fail(key, f"worker error: {type(e).__name__}: {e}", {})
        finally:
            hb_stop.set()
            hb.join(timeout=10)
            if workspace and not self.config.keep_workspaces:
                remove_workspace(workspace)

    def _try_record_cost(self, execution_id: int, outcome: RunOutcome) -> None:
        if outcome.cost_usd is None:
            return
        try:
            self.api.record_execution_cost(execution_id, outcome.cost_payload())
        except (ApiError, OSError) as e:
            log.warning("execution %s: could not record its cost: %s", execution_id, e)

    def _request(self, task: dict[str, Any], ws: Workspace, project: dict[str, Any]) -> RunRequest:
        agent = self.api.agent(self.config.agent_key)
        feedback = feedback_from_events(self.api.events(task["key"]))
        return RunRequest(
            task_key=task["key"],
            title=task["title"],
            specification=task["specification"],
            acceptance_criteria=task["acceptance_criteria"],
            allowed_paths=task["allowed_paths"],
            workspace=ws.repo,
            output_dir=ws.out,
            budget_usd=Decimal(str(task["run_budget_usd"])),
            model=agent.get("model") or self.config.default_model,
            effort=self.config.effort,
            previous_feedback=feedback,
            round=task["review_rounds"] + 1,
            python_version=project.get("python_version"),
        )

    def _heartbeat(
        self, key: str, stop: threading.Event, lost: threading.Event, runtime: Runtime
    ) -> None:
        while not stop.wait(self.config.heartbeat_seconds):
            try:
                self.api.heartbeat(key, self.config.lease_seconds)
            except ApiError as e:
                if e.status in (403, 409):  # lease expired / not ours any more
                    log.warning("%s: heartbeat rejected (%s); cancelling run", key, e.code)
                    lost.set()
                    runtime.cancel()
                    return
                log.warning("%s: heartbeat failed: %s", key, e)
            except OSError as e:  # API briefly unreachable: keep trying until the lease ends
                log.warning("%s: heartbeat error: %s", key, e)

    # ---- reporting -----------------------------------------------------------------------

    def _report(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        execution_id: int,
        ws: Workspace,
        outcome: RunOutcome,
    ) -> None:
        key = task["key"]
        cost = {"cost": outcome.cost_payload()}
        if outcome.transcript_path:
            cost["transcript_uri"] = outcome.transcript_path.as_uri()  # type: ignore[assignment]
        if outcome.outcome in (Outcome.FAILED, Outcome.BUDGET_EXCEEDED, Outcome.CANCELLED):
            self._try_fail(key, outcome.error or outcome.outcome.value, cost)
            return
        assert outcome.result is not None
        result = outcome.result.model_dump(mode="json")
        if outcome.outcome == Outcome.NEEDS_INPUT:
            self.api.command(
                key, "ask_human", {"question": outcome.result.question} | result | cost
            )
            return

        check = check_changes(ws.repo, ws.base_sha, task["allowed_paths"])
        if not check.ok:
            self._try_fail(key, "verification failed: " + "; ".join(check.problems),
                           cost | {"problems": check.problems})  # fmt: skip
            return
        # The agent's own words become the commit message and PR body. The diff check doesn't see
        # them, and an agent tricked into printing its environment would publish its API key.
        leaked = credentials_in(json.dumps(result))
        if leaked:
            kinds = ", ".join(leaked)
            self._try_fail(key, f"verification failed: result text looks like a {kinds}", cost)
            return
        sha = commit(
            ws.repo,
            f"{key}: {task['title']}\n\n{outcome.result.summary}\n\n"
            f"Axiom execution e{execution_id} by agent {self.config.agent_key}.",
            f"Axiom agent {self.config.agent_key} <axiom@localhost>",
        )
        verified = None
        if project.get("test_command"):
            tests = run_tests(ws.repo, ws.root / "verify", test_command(project),
                              self.test_runner, self.config.test_timeout_seconds)  # fmt: skip
            verified = {"command": tests.command, "exit_code": tests.exit_code,
                        "passed": tests.passed, "output_tail": tests.output_tail}  # fmt: skip
            if not tests.passed:
                self._try_fail(key, f"verified tests failed (exit {tests.exit_code})",
                               cost | {"verified_tests": verified})  # fmt: skip
                return

        push_branch(ws.repo, ws.branch, self.config.github_token, project["repo_url"])
        body = pr_body(
            task_key=key, title=task["title"], agent=self.config.agent_key,
            execution_id=execution_id, result=result, verified_tests=verified,
            cost_usd=cost["cost"]["usd"],
        )  # fmt: skip
        pr = self.open_pr(project["repo_url"], ws.branch, project["default_branch"],
                          f"{key}: {task['title']}", body)  # fmt: skip
        if not pr.created:  # rework: same PR, so explain what this round changed
            self.comment_pr(
                project["repo_url"],
                pr.number,
                round_comment(
                    task, execution_id, self.config.agent_key, result, verified, cost["cost"]["usd"]
                ),
            )
        self.api.command(
            key,
            "submit",
            result
            | cost
            | {
                "verified_tests": verified,
                "commit": sha,
                "branch": ws.branch,
                "pr_url": pr.url,
            },
        )
        log.info("%s: submitted %s", key, pr.url)

    def _try_fail(self, key: str, error: str, extra: dict[str, Any]) -> None:
        try:
            self.api.command(key, "fail", {"error": error[:2000]} | extra)
        except ApiError as e:
            log.warning("%s: could not report failure (%s)", key, e)


def round_comment(
    task: dict[str, Any],
    execution_id: int,
    agent: str,
    result: dict[str, Any],
    verified: dict[str, Any] | None,
    cost_usd: str | None,
) -> str:
    """PR comment for a rework round: what changed, and the verified test result."""
    lines = [
        f"**Round {task['review_rounds'] + 1}** (agent `{agent}`, execution e{execution_id})",
        "",
        result.get("summary", ""),
    ]
    if verified:
        status = "✅ passed" if verified["passed"] else "❌ failed"
        lines += ["", f"Tests re-run by Axiom: `{verified['command']}` {status}"]
    lines += ["", f"Cost of this round: ${cost_usd or '?'}. Reviews for this round are starting."]
    return "\n".join(lines)
