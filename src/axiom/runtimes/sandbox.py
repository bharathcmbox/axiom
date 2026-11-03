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
from threading import Lock

from axiom.runtimes.base import (
    EventCallback,
    Outcome,
    RunOutcome,
    RunRequest,
    outcome_from_dict,
    request_to_dict,
)

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
    docker: str = "docker"
    name: str = "sandbox"
    _container: str | None = None
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False

    def docker_args(
        self, request: RunRequest, container: str, entrypoint: list[str] | None = None
    ) -> list[str]:
        st = request.workspace.stat()
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
            "--tmpfs", "/home/agent:rw,size=2g,mode=1777",
            "-e", "HOME=/home/agent",
            "-e", f"HTTPS_PROXY={self.proxy_url}",
            "-e", f"HTTP_PROXY={self.proxy_url}",
            "-e", "NO_PROXY=",
            "-v", f"{request.workspace}:/workspace/repo:rw",
            "-v", f"{request.output_dir}:/workspace/out:rw",
        ]  # fmt: skip
        for var in PASSTHROUGH_ENV:
            if var in os.environ:
                args += ["-e", var]  # name only: the value is read from our env, not argv
        if self.fake_scenario:
            args += ["-e", f"AXIOM_FAKE_SCENARIO={self.fake_scenario}"]
        if entrypoint:
            args += ["--entrypoint", entrypoint[0], self.image, *entrypoint[1:]]
        else:
            args.append(self.image)
        return args

    def run(self, request: RunRequest, on_event: EventCallback | None = None) -> RunOutcome:
        emit: Callable[[dict[str, object]], None] = on_event or (lambda e: None)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "request.json").write_text(json.dumps(request_to_dict(request)))
        container = f"axiom-sbx-{request.task_key.lower()}-{secrets.token_hex(4)}"
        with self._lock:
            if self._cancelled:
                return RunOutcome(Outcome.CANCELLED, error="cancelled before start")
            self._container = container
        emit({"type": "sandbox_started", "container": container})
        log_path = request.output_dir / "sandbox.log"
        with log_path.open("w") as log:
            proc = subprocess.Popen(
                self.docker_args(request, container), stdout=log, stderr=subprocess.STDOUT
            )
            try:
                code = proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._kill(container)
                proc.wait(timeout=60)
                return RunOutcome(Outcome.FAILED, error=f"timed out after {self.timeout_seconds}s")
        emit({"type": "sandbox_exited", "container": container, "code": code})
        if self._cancelled:
            return RunOutcome(Outcome.CANCELLED, error="cancelled")
        outcome_file = request.output_dir / "outcome.json"
        if not outcome_file.exists():
            tail = log_path.read_text(errors="replace")[-2000:]
            return RunOutcome(
                Outcome.FAILED, error=f"sandbox exited {code} without outcome: {tail}"
            )
        return outcome_from_dict(json.loads(outcome_file.read_text()), request.output_dir)

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
