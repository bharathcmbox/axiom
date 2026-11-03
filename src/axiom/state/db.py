"""Database connections. Rows are returned as dicts."""

from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

type Conn = psycopg.Connection[DictRow]


def connect(url: str) -> Conn:
    """Autocommit connection; services open explicit `conn.transaction()` blocks."""
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def create_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool[Conn]:
    kwargs: dict[str, Any] = {"autocommit": True, "row_factory": dict_row}
    return ConnectionPool(
        url,
        connection_class=psycopg.Connection[DictRow],
        kwargs=kwargs,
        min_size=min_size,
        max_size=max_size,
        open=True,
    )
