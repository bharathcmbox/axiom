"""FastAPI application factory.

Startup: open the pool, apply migrations, report invariant violations, run one reaper sweep
(leases that expired while Axiom was down are timed out immediately), start the reaper thread.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from axiom.api import errors
from axiom.api.routes import router
from axiom.config import Settings, get_settings
from axiom.orchestrator.reaper import Reaper
from axiom.state.db import create_pool
from axiom.state.invariants import check_invariants
from axiom.state.migrate import migrate

log = logging.getLogger("axiom")


def create_app(settings: Settings | None = None, *, start_reaper: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings.database_url)
        reaper = Reaper(pool, settings.reaper_interval_seconds)
        try:
            with pool.connection() as conn:
                applied = migrate(conn)
                if applied:
                    log.info("applied migrations: %s", ", ".join(applied))
                for violation in check_invariants(conn):
                    log.error("invariant violation at startup: %s", violation)
            recovered = reaper.sweep()
            if recovered:
                log.warning("startup: timed out %d stale execution(s)", len(recovered))
            if start_reaper:
                reaper.start()
            app.state.pool = pool
            yield
        finally:
            if start_reaper:
                reaper.stop()
            pool.close()

    app = FastAPI(
        title="Axiom",
        summary="Control plane for an AI engineering organization",
        version="0.1.0",
        lifespan=lifespan,
    )
    errors.install(app)
    app.include_router(router)
    return app


def main() -> None:  # `python -m axiom.api.app`
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
