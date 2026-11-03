"""Apply numbered SQL migrations from the migrations/ directory.

Each file is named NNNN_description.sql and is applied at most once, recorded in
`schema_migrations`. All pending migrations run in one transaction under an advisory lock, so
concurrent starters (several API processes, tests) cannot apply the same migration twice.
"""

import re
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import tuple_row

from axiom.config import get_settings

MIGRATION_LOCK_ID = 7_351_001  # arbitrary, stable advisory-lock key for migrations
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def default_migrations_dir() -> Path:
    configured = get_settings().migrations_dir
    if configured:
        return Path(configured)
    # src/axiom/state/migrate.py -> repository root
    return Path(__file__).resolve().parents[3] / "migrations"


def discover(migrations_dir: Path) -> list[Path]:
    files = sorted(migrations_dir.glob("*.sql"))
    bad = [f.name for f in files if not _NAME.match(f.name)]
    if bad:
        raise ValueError(f"migration files must be named NNNN_description.sql: {bad}")
    versions = [f.name[:4] for f in files]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration numbers in {migrations_dir}")
    return files


def migrate(conn: psycopg.Connection[Any], migrations_dir: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the names applied (empty when up to date)."""
    files = discover(migrations_dir or default_migrations_dir())
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 name       text PRIMARY KEY,
                 applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        with conn.cursor(row_factory=tuple_row) as cur:  # works for any connection row type
            done = {row[0] for row in cur.execute("SELECT name FROM schema_migrations")}
        for path in files:
            name = path.stem
            if name in done:
                continue
            # No parameters -> simple query protocol, so a file may hold many statements.
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied_now.append(name)
    return applied_now


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_url
    with psycopg.connect(url) as conn:
        applied = migrate(conn)
    print("applied: " + ", ".join(applied) if applied else "up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
