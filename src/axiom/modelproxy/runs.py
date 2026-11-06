"""Runs: what one run token may do, and what it has used. No I/O, so every rule can be tested alone.

Held in memory on purpose: if the proxy restarts, tokens die and runs fail and retry. The worst
outcome of losing this state is a failed run, never an open door.
"""

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from axiom.runtimes.estimate import usage_cost

RETENTION_SECONDS = 24 * 3600  # a restarted worker can still ask what a killed run used
USAGE_COUNTS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Run:
    id: str
    token: str
    budget_usd: Decimal
    model: str | None
    expires_at: float
    closed_at: float | None = None
    requests: int = 0
    budget_refusals: int = 0
    cost_usd: Decimal = Decimal(0)
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(USAGE_COUNTS, 0))

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "requests": self.requests,
            "budget_refusals": self.budget_refusals,
            "cost_usd": str(self.cost_usd),
            "input_tokens": sum(v for k, v in self.usage.items() if k != "output_tokens"),
            "output_tokens": self.usage["output_tokens"],
        }


class Runs:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, Run] = {}
        self._by_token: dict[str, Run] = {}

    def open(self, budget_usd: Decimal, model: str | None, ttl_seconds: float) -> Run:
        if budget_usd <= 0:
            raise Refused(422, "budget_usd must be above zero")
        run = Run(
            id=f"mr_{secrets.token_hex(8)}",
            token=f"run_{secrets.token_urlsafe(32)}",
            budget_usd=budget_usd,
            model=model,
            expires_at=self._clock() + ttl_seconds,
        )
        with self._lock:
            self._purge()
            self._by_id[run.id], self._by_token[run.token] = run, run
        return run

    def _get(self, run_id: str) -> Run:
        run = self._by_id.get(run_id)
        if run is None:
            raise Refused(404, "unknown run")
        return run

    def extend(self, run_id: str, ttl_seconds: float) -> None:
        """A lapsed run stays dead: if its worker stopped renewing, the sandbox is on its own."""
        with self._lock:
            run = self._get(run_id)
            if run.closed_at is not None or run.expires_at <= self._clock():
                raise Refused(409, "run has ended")
            run.expires_at = self._clock() + ttl_seconds

    def close(self, run_id: str) -> dict[str, Any]:
        """Ends the run and reports its use. Closing twice gives the same answer."""
        with self._lock:
            run = self._get(run_id)
            if run.closed_at is None:
                run.closed_at = self._clock()
            return run.summary()

    def admit(self, token: str | None, model: Any) -> Run:
        """May this request go out? A request in flight may overshoot, by at most its own cost."""
        with self._lock:
            run = self._by_token.get(token or "")
            if run is None or run.closed_at is not None or run.expires_at <= self._clock():
                raise Refused(401, "run token is not valid")
            if run.model is not None and model != run.model:
                raise Refused(403, f"this run may only use {run.model}")
            if run.cost_usd >= run.budget_usd:
                run.budget_refusals += 1
                raise Refused(403, "run budget exhausted")
            run.requests += 1
            return run

    def record(self, run: Run, model: str | None, usage: dict[str, Any]) -> None:
        with self._lock:
            run.cost_usd += usage_cost(model, usage)
            for key in USAGE_COUNTS:
                value = usage.get(key)
                if isinstance(value, int) and value > 0:
                    run.usage[key] += value

    def _purge(self) -> None:
        cutoff = self._clock() - RETENTION_SECONDS
        for run in [r for r in self._by_id.values() if r.expires_at < cutoff]:
            del self._by_id[run.id], self._by_token[run.token]
