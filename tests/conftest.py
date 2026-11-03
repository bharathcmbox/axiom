from collections.abc import Iterator

import psycopg
import pytest

from axiom.config import get_settings


@pytest.fixture
def test_db_url() -> str:
    return get_settings().test_database_url


@pytest.fixture
def conn(test_db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(test_db_url) as c:
        yield c
