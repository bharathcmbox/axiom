"""Shared fixtures. Integration tests run against the real `axiom_test` Postgres database.

The schema is rebuilt from migrations once per test session; every test starts from empty
tables (TRUNCATE ... RESTART IDENTITY).
"""

from collections.abc import Iterator

import psycopg
import pytest

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.state.db import Conn, connect
from axiom.state.migrate import migrate

APP_TABLES = ("events", "executions", "task_dependencies", "tasks", "agents", "projects")


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
    """Dict-row autocommit connection, as used by the services."""
    with connect(db_url) as c:
        yield c


@pytest.fixture
def human() -> Actor:
    return Actor(ActorKind.HUMAN, "tester")
