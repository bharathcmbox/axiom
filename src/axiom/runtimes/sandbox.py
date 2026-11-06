"""SandboxRuntime: runs the agent in a fresh, locked-down container (ADR-008).

The harness side of the sandbox. It writes request.json into the execution's out directory,
starts `axiom-sandbox` with only the workspace and out directory mounted, waits (with a
timeout), and reads outcome.json back. It passes no credentials at all. The agent reaches the model
through the model proxy with a run token that is useless anywhere else (ADR-015), and what the run
cost comes from the proxy's count, not from anything the sandbox wrote.

Paths given to `docker run -v` are interpreted by the Docker daemon on the host. When the
harness itself runs in a container, the workspaces directory must be bind-mounted at the
same absolute path in the harness container as on the host.
"""

import dataclasses
import json
import logging
import secrets
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from axiom.modelproxy.client import Metered, ModelProxyClient, ModelProxyError
from axiom.runtimes.base import (
    CommandResult,
    EventCallback,
    Outcome,
    RunOutcome,
    RunRequest,
    outcome_from_dict,
    request_to_dict,
)
from axiom.runtimes.review import (
    ReviewOutcome,
    ReviewRequest,
    review_outcome_from_dict,
    review_request_to_dict,
)

log = logging.getLogger("axiom.sandbox")
MODEL_RUN_FILE = "model-run.json"
CANARY_ENV = "INTERNAL_DEPLOY_TOKEN"  # see axiom.drills.injection


@dataclass
class _ModelRun:
    env: list[str] = field(default_factory=list)
    metered: Metered | None = None


def _settle[Reported: (RunOutcome, ReviewOutcome)](
    outcome: Reported, metered: Metered | None
) -> Reported:
    """The proxy's count is the record. Whatever the sandbox wrote about its cost is ignored."""
    if metered is None:
        return outcome
    settled = dataclasses.replace(
        outcome,
        cost_usd=metered.cost_usd,
        input_tokens=metered.input_tokens or None,
        output_tokens=metered.output_tokens,
        cost_estimated=False,
    )
    if metered.budget_refusals and outcome.outcome != Outcome.COMPLETED:
        settled = dataclasses.replace(
            settled, outcome=Outcome.BUDGET_EXCEEDED, error="budget exceeded"
        )
    return settled


@dataclass
class SandboxRuntime:
    image: str = "axiom-sandbox:latest"
    network: str = "axiom_sandbox"
    proxy_url: str = "http://egress-proxy:3128"
    timeout_seconds: int = 3600
    memory: str = "4g"
    cpus: str = "2"
    fake_scenario: str | None = None  # run FakeRuntime inside the container (tests, demos)
    fake_review: str | None = None  # run FakeReviewer inside the container (tests, demos)
    docker: str = "docker"
    drill_canary: str | None = None  # a fake secret for the injection drill; never a real one
    model_proxy: ModelProxyClient | None = None  # how real runs reach the model
    model_ttl_seconds: float = 120
    name: str = "sandbox"
    _container: str | None = None
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False

    def docker_args(
        self,
        request: RunRequest,
        container: str,
        entrypoint: list[str] | None = None,
    ) -> list[str]:
        return self._args(request.workspace, request.output_dir, container, entrypoint)

    def _args(
        self,
        workspace: Path,
        output_dir: Path,
        container: str,
        entrypoint: list[str] | None,
        env: list[str] | None = None,
    ) -> list[str]:
        st = workspace.stat()
        args = [
            self.docker, "run", "--rm", "--name", container,
            "--network", self.network,
            "--user", f"{st.st_uid}:{st.st_gid}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "512",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--read-only",
            "--tmpfs", "/tmp:rw,size=1g,mode=1777",
            # exec: uv installs tools under the cache here (`uv run --with`), and Docker's tmpfs
            # default is noexec. No loss: the workspace mount is executable anyway.
            "--tmpfs", "/home/agent:rw,exec,size=2g,mode=1777",
            "-e", "HOME=/home/agent",
            "-e", f"HTTPS_PROXY={self.proxy_url}",
            "-e", f"HTTP_PROXY={self.proxy_url}",
            "-e", "NO_PROXY=",
            "-v", f"{workspace}:/workspace/repo:rw",
            "-v", f"{output_dir}:/workspace/out:rw",
        ]  # fmt: skip
        for item in env or []:
            args += ["-e", item]
        if entrypoint:
            args += ["--entrypoint", entrypoint[0], self.image, *entrypoint[1:]]
        else:
            args.append(self.image)
        return args

    def run(self, request: RunRequest, on_event: EventCallback | None = None) -> RunOutcome:
        """A developer run."""
        env = [f"AXIOM_FAKE_SCENARIO={self.fake_scenario}"] if self.fake_scenario else []
        env += self._drill_env()
        if request.python_version:
            env.append(f"UV_PYTHON={request.python_version}")
        with self._model_run(request.budget_usd, request.model, request.output_dir) as run:
            done = self._run_container(
                request_json=request_to_dict(request),
                workspace=request.workspace,
                output_dir=request.output_dir,
                task_key=request.task_key,
                outcome_name="outcome.json",
                extra_env=env + run.env,
                on_event=on_event,
            )
        outcome = (
            done if isinstance(done, RunOutcome) else outcome_from_dict(done, request.output_dir)
        )
        return _settle(outcome, run.metered)

    def run_review(
        self, request: ReviewRequest, on_event: EventCallback | None = None
    ) -> ReviewOutcome:
        """A read-only review run (the runner switches on request.json's `mode`)."""
        env = [f"AXIOM_FAKE_REVIEW={self.fake_review}"] if self.fake_review else []
        with self._model_run(request.budget_usd, request.model, request.output_dir) as run:
            done = self._run_container(
                request_json=review_request_to_dict(request),
                workspace=request.workspace,
                output_dir=request.output_dir,
                task_key=request.task_key,
                outcome_name="review_outcome.json",
                extra_env=env + self._drill_env() + run.env,
                on_event=on_event,
            )
        outcome = (
            ReviewOutcome(done.outcome, error=done.error)
            if isinstance(done, RunOutcome)
            else review_outcome_from_dict(done, request.output_dir)
        )
        return _settle(outcome, run.metered)

    @contextmanager
    def _model_run(
        self, budget: Decimal, model: str | None, output_dir: Path
    ) -> Iterator[_ModelRun]:
        """Open a run on the proxy, keep it alive while the sandbox runs, close it, read its use.
        If this worker dies, renewal stops and the sandbox loses the model within one expiry."""
        run = _ModelRun()
        proxy = self.model_proxy
        if proxy is None:  # fake runs, which never call a model
            yield run
            return
        grant = proxy.open(budget, model, self.model_ttl_seconds)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Beside out/, where the sandbox can't change it: a restarted worker can ask the proxy.
        (output_dir.parent / MODEL_RUN_FILE).write_text(json.dumps({"run_id": grant.run_id}))
        run.env = [
            f"ANTHROPIC_BASE_URL={proxy.sandbox_address}",
            f"ANTHROPIC_API_KEY={grant.token}",
            f"NO_PROXY={proxy.sandbox_host}",
        ]
        stop = Event()
        renewal = Thread(target=self._keep_alive, args=(grant.run_id, stop), daemon=True)
        renewal.start()
        try:
            yield run
        finally:
            stop.set()
            renewal.join(timeout=30)
            try:
                run.metered = proxy.close(grant.run_id)
            except ModelProxyError as e:
                log.warning("could not read what run %s used: %s", grant.run_id, e)

    def _keep_alive(self, run_id: str, stop: Event) -> None:
        assert self.model_proxy is not None
        while not stop.wait(self.model_ttl_seconds / 3):
            try:
                self.model_proxy.extend(run_id, self.model_ttl_seconds)
            except ModelProxyError as e:  # a brief failure is fine: the expiry allows for two
                log.warning("could not renew run %s: %s", run_id, e)

    def _drill_env(self) -> list[str]:
        return [f"{CANARY_ENV}={self.drill_canary}"] if self.drill_canary else []

    def _run_container(
        self,
        *,
        request_json: dict[str, Any],
        workspace: Path,
        output_dir: Path,
        task_key: str,
        outcome_name: str,
        extra_env: list[str],
        on_event: EventCallback | None,
    ) -> dict[str, Any] | RunOutcome:
        """Start the sandbox, wait, and return the parsed outcome file (or a failure)."""
        emit: Callable[[dict[str, object]], None] = on_event or (lambda e: None)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "request.json").write_text(json.dumps(request_json))
        container = f"axiom-sbx-{task_key.lower()}-{secrets.token_hex(4)}"
        with self._lock:
            if self._cancelled:
                return RunOutcome(Outcome.CANCELLED, error="cancelled before start")
            self._container = container
        emit({"type": "sandbox_started", "container": container})
        args = self._args(workspace, output_dir, container, None, env=extra_env)
        log_path = output_dir / "sandbox.log"
        with log_path.open("w") as log:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._kill(container)
                proc.wait(timeout=60)
                return RunOutcome(Outcome.FAILED, error=f"timed out after {self.timeout_seconds}s")
        emit({"type": "sandbox_exited", "container": container, "code": code})
        if self._cancelled:
            return RunOutcome(Outcome.CANCELLED, error="cancelled")
        outcome_file = output_dir / outcome_name
        if not outcome_file.exists():
            tail = log_path.read_text(errors="replace")[-2000:]
            return RunOutcome(
                Outcome.FAILED, error=f"sandbox exited {code} without outcome: {tail}"
            )
        data: dict[str, Any] = json.loads(outcome_file.read_text())
        return data

    def exec(self, workdir: Path, outdir: Path, command: str, timeout: int) -> CommandResult:
        """Run a shell command (e.g. the project's tests) in a fresh sandbox, without any
        credentials at all: verification needs no API key."""
        container = f"axiom-verify-{secrets.token_hex(4)}"
        args = self._args(workdir, outdir, container, ["sh", "-c", command])
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(container)
            return CommandResult(exit_code=124, output=f"timed out after {timeout}s")
        return CommandResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            container = self._container
        if container:
            self._kill(container)

    def _kill(self, container: str) -> None:
        subprocess.run(
            [self.docker, "kill", container], capture_output=True, timeout=60, check=False
        )
