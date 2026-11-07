"""Background thread that periodically times out expired leases."""

import logging
import threading

from psycopg_pool import ConnectionPool

from axiom.orchestrator.leases import expire_leases
from axiom.state.db import Conn

log = logging.getLogger(__name__)


class Reaper:
    def __init__(self, pool: ConnectionPool[Conn], interval_seconds: float) -> None:
        self._pool = pool
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="axiom-reaper", daemon=True)

    def sweep(self) -> list[str]:
        """Time out expired leases: executions, reviews and plans."""
        from axiom.orchestrator.plans import expire_plan_leases
        from axiom.orchestrator.reviews import expire_review_leases

        with self._pool.connection() as conn:
            tasks = expire_leases(conn)
            reviews = expire_review_leases(conn)
            plans = expire_plan_leases(conn)
        return tasks + [f"review:{r}" for r in reviews] + [f"plan:{p}" for p in plans]

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:  # keep sweeping; a transient DB error must not kill the reaper
                log.exception("reaper sweep failed")
            self._stop.wait(self._interval)
