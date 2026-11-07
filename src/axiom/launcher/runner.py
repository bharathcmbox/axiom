"""Start a validated container, wait for it, and return its output.

Output goes to a file outside the sandbox's reach and comes back in the result, so nobody ever
reads a log from the folder the sandbox can write to. If the caller goes away, the container is
killed: a killed worker leaves no sandbox behind.
"""

import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from axiom.launcher.spec import ContainerSpec, Policy, docker_args, validate

OUTPUT_LIMIT = 64 * 1024
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class LaunchResult:
    exit_code: int
    output: str = ""
    timed_out: bool = False
    abandoned: bool = False


class Launcher(Protocol):
    def run(self, spec: ContainerSpec, timeout_seconds: float) -> LaunchResult: ...

    def kill(self, name: str) -> bool: ...


@dataclass
class LocalLauncher:
    policy: Policy = field(default_factory=Policy)
    _running: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(
        self,
        spec: ContainerSpec,
        timeout_seconds: float,
        caller_gone: Callable[[], bool] | None = None,
    ) -> LaunchResult:
        validate(spec, self.policy)
        with self._lock:
            self._running.add(spec.name)
        try:
            with tempfile.TemporaryFile() as log:
                proc = subprocess.Popen(
                    docker_args(spec, self.policy), stdout=log, stderr=subprocess.STDOUT
                )
                result = self._wait(proc, spec.name, timeout_seconds, caller_gone)
                log.seek(max(0, log.seek(0, 2) - OUTPUT_LIMIT))
                output = log.read().decode(errors="replace")
        finally:
            with self._lock:
                self._running.discard(spec.name)
        return LaunchResult(result[0], output, timed_out=result[1], abandoned=result[2])

    def _wait(
        self,
        proc: subprocess.Popen[bytes],
        name: str,
        timeout_seconds: float,
        caller_gone: Callable[[], bool] | None,
    ) -> tuple[int, bool, bool]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                return proc.wait(timeout=POLL_SECONDS), False, False
            except subprocess.TimeoutExpired:
                pass
            expired = time.monotonic() >= deadline
            gone = caller_gone is not None and caller_gone()
            if expired or gone:
                self._docker_kill(name)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                return 124, expired, gone

    def kill(self, name: str) -> bool:
        """Only containers this launcher started, so a request can't kill anything else."""
        with self._lock:
            if name not in self._running:
                return False
        self._docker_kill(name)
        return True

    def _docker_kill(self, name: str) -> None:
        subprocess.run(
            [self.policy.docker, "kill", name], capture_output=True, timeout=60, check=False
        )
