"""Runtime configuration, read from environment variables prefixed with AXIOM_."""

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AXIOM_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://axiom:axiom@127.0.0.1:5432/axiom"
    test_database_url: str = "postgresql://axiom:axiom@127.0.0.1:5432/axiom_test"
    migrations_dir: str | None = None  # default: <repo>/migrations
    # Lease granted on claim/assign/start and extended by heartbeats. Generous by default so
    # tasks can be driven by hand with curl in Phase 1.
    lease_seconds: int = 300
    # How often the reaper looks for expired leases.
    reaper_interval_seconds: float = 5.0
    # Global cap on concurrently active developer executions (SPEC: 2 concurrent developers).
    max_concurrent_developers: int = 2
    # Budgets (SPEC principle 8): default per-task budget, and the daily cap across all work.
    default_task_budget_usd: Decimal = Decimal("3.00")
    daily_budget_usd: Decimal = Decimal("15.00")
    # The most one developer run may reserve, so one run can't hold the whole day's budget.
    max_run_budget_usd: Decimal = Decimal("6.00")
    # Reviews (ADR-009): roles required for every round, per-review cost cap, round cap.
    required_review_roles: tuple[str, ...] = ("code_reviewer", "security_reviewer")
    review_budget_usd: Decimal = Decimal("1.00")
    max_review_rounds: int = 3
    review_lease_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
