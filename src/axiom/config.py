"""Runtime configuration, read from environment variables prefixed with AXIOM_."""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
