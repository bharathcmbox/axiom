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
    # A review run gets its share of what the task has left, at least review_budget_usd and at
    # most review_max_run_usd (the exit run's 537-line test PR needed more than $1).
    review_budget_usd: Decimal = Decimal("1.00")
    review_max_run_usd: Decimal = Decimal("2.50")
    # Below this a review may not finish. Fable reviews have cost up to $0.64 so far, and the SDK
    # stops at 80% of a run's budget: 80% of $0.85 is $0.68.
    review_min_run_usd: Decimal = Decimal("0.85")
    max_review_rounds: int = 3
    review_lease_seconds: int = 300
    # Plans (ADR-011): the most one planning attempt may spend, and its lease.
    plan_run_budget_usd: Decimal = Decimal("1.00")
    plan_min_run_usd: Decimal = Decimal("0.50")
    plan_lease_seconds: int = 300
    # Follow-ups (ADR-018): how long something may wait for a human before one reminder.
    follow_up_waiting_seconds: int = 86_400  # a question or an escalation
    follow_up_merge_seconds: int = 172_800  # a PR that passed review and CI
    follow_up_plan_seconds: int = 86_400  # a plan waiting for approval
    max_timers_per_sweep: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
