"""SandboxRuntime: runs the agent in a fresh, locked-down container (ADR-008).

The harness side of the sandbox. It writes request.json into the execution's out directory,
starts `axiom-sandbox` with only the workspace and out directory mounted, waits (with a
timeout), and reads outcome.json back. It never passes GitHub or Axiom credentials.

Paths given to `docker run -v` are interpreted by the Docker daemon on the host. When the
harness itself runs in a container, the workspaces directory must be bind-mounted at the
same absolute path in the harness container as on the host.
"""

import json
import os
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

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

CANARY_ENV = "INTERNAL_DEPLOY_TOKEN"  # see axiom.drills.injection

# The only environment variables a sandbox may receive from the harness's environment.
PASSTHROUGH_ENV = ("ANTHROPIC_API_KEY",)


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
    name: str = "sandbox"
    _container: str | None = None
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False

    def docker_args(
        self,
        request: RunRequest,
        container: str,
        entrypoint: list[str] | None = None,
        *,
        pass_env: bool = True,
    ) -> list[str]:
        return self._args(request.workspace, request.output_dir, container, entrypoint, pass_env)

    def _args(
        self,
        workspace: Path,
        output_dir: Path,
        container: str,
        entrypoint: list[str] | None,
        pass_env: bool,
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
        for var in PASSTHROUGH_ENV if pass_env else ():
            if var in os.environ:
                args += ["-e", var]  # name only: the value is read from our env, not argv
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
        done = self._run_container(
            request_json=request_to_dict(request),
            workspace=request.workspace,
            output_dir=request.output_dir,
            task_key=request.task_key,
            outcome_name="outcome.json",
            extra_env=env,
            on_event=on_event,
        )
        if isinstance(done, RunOutcome):
            return done
        return outcome_from_dict(done, request.output_dir)

    def run_review(
        self, request: ReviewRequest, on_event: EventCallback | None = None
    ) -> ReviewOutcome:
        """A read-only review run (the runner switches on request.json's `mode`)."""
        done = self._run_container(
            request_json=review_request_to_dict(request),
            workspace=request.workspace,
            output_dir=request.output_dir,
            task_key=request.task_key,
            outcome_name="review_outcome.json",
            extra_env=[f"AXIOM_FAKE_REVIEW={self.fake_review}"] * bool(self.fake_review)
            + self._drill_env(),
            on_event=on_event,
        )
        if isinstance(done, RunOutcome):
            return ReviewOutcome(done.outcome, error=done.error)
        return review_outcome_from_dict(done, request.output_dir)

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
        args = self._args(workspace, output_dir, container, None, pass_env=True, env=extra_env)
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
        args = self._args(workdir, outdir, container, ["sh", "-c", command], pass_env=False)
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
