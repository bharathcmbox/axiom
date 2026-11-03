"""Shared fixtures. Integration tests run against the real `axiom_test` Postgres database.

The schema is rebuilt from migrations once per test session; every test starts from empty
tables (TRUNCATE ... RESTART IDENTITY).
"""

from collections.abc import Iterator

import psycopg
import pytest

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.registry.agents import seed_agents
from axiom.registry.projects import create_project
from axiom.registry.seed import DEFAULT_CONFIG
from axiom.registry.tokens import hash_token
from axiom.state.db import Conn, connect
from axiom.state.invariants import check_invariants
from axiom.state.migrate import migrate

APP_TABLES = (
    "events", "api_tokens", "executions", "task_dependencies", "tasks", "agents", "projects",
)  # fmt: skip


def reset_schema(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    with psycopg.connect(url) as c:
        migrate(c)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = get_settings().test_database_url
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"refusing to reset non-test DB: {url}"
    return url


@pytest.fixture(scope="session")
def migrated_db(test_db_url: str) -> str:
    reset_schema(test_db_url)
    return test_db_url


@pytest.fixture
def db_url(migrated_db: str) -> Iterator[str]:
    """A migrated test database with empty tables."""
    with psycopg.connect(migrated_db, autocommit=True) as c:
        c.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")
    yield migrated_db


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection: each statement commits, so constraint errors don't poison it."""
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c


@pytest.fixture
def db(db_url: str) -> Iterator[Conn]:
    """Dict-row autocommit connection, as used by the services.

    On teardown, asserts the cross-table invariants still hold.
    """
    with connect(db_url) as c:
        yield c
    with connect(db_url) as c:
        assert check_invariants(c) == []


@pytest.fixture
def human() -> Actor:
    return Actor(ActorKind.HUMAN, "tester")


@pytest.fixture
def org(db: Conn, human: Actor) -> Conn:
    """Project AX plus the 7-agent organization from config/agents.toml."""
    create_project(db, human, key="AX", name="Axiom")
    seed_agents(db, human, DEFAULT_CONFIG)
    return db


TEST_TOKENS = {
    "human:bharath": "axm_test-human-bharath",
    "agent:DEV-01": "axm_test-agent-dev01",
    "agent:DEV-02": "axm_test-agent-dev02",
}


def bearer(actor: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKENS[actor]}"}


@pytest.fixture
def api_tokens(org: Conn) -> Conn:
    """Known test tokens (hash stored, as in production) for a human and two developers."""
    for actor, token in TEST_TOKENS.items():
        agent = actor.split(":", 1)[1] if actor.startswith("agent:") else None
        org.execute(
            """INSERT INTO api_tokens (token_hash, actor, agent_id)
               VALUES (%s, %s, (SELECT id FROM agents WHERE key = %s))""",
            (hash_token(token), actor, agent),
        )
    return org
