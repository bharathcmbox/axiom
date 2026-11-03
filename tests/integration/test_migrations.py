"""T1: migrations apply cleanly, are idempotent, and the schema enforces its invariants."""

from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from axiom.state.migrate import default_migrations_dir, discover, migrate
from tests.conftest import reset_schema


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {r[0] for r in rows}


def test_fresh_database_gets_full_schema_and_rerun_is_noop(migrated_db: str) -> None:
    reset_schema(migrated_db)  # leaves the DB migrated, like the session fixture
    with psycopg.connect(migrated_db) as c:
        assert _tables(c) >= {
            "schema_migrations",
            "projects",
            "agents",
            "tasks",
            "task_dependencies",
            "executions",
            "events",
        }
        assert [r[0] for r in c.execute("SELECT name FROM schema_migrations")] == ["0001_init"]
        assert migrate(c) == []  # second run applies nothing


def test_discover_rejects_badly_named_files(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1")
    with pytest.raises(ValueError, match="NNNN_description"):
        discover(tmp_path)


def test_discover_rejects_duplicate_numbers(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    (tmp_path / "0001_b.sql").write_text("SELECT 1")
    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)


def test_repository_migrations_are_well_formed() -> None:
    assert discover(default_migrations_dir())[0].name == "0001_init.sql"


# ---- constraint tests -------------------------------------------------------------------


@pytest.fixture
def task_and_agent(conn: psycopg.Connection) -> tuple[int, int]:
    conn.execute("INSERT INTO projects (key, name) VALUES ('AX', 'Axiom')")
    conn.execute(
        "INSERT INTO agents (key, role, runtime) VALUES ('DEV-01', 'developer', 'claude_code')"
    )
    conn.execute(
        """INSERT INTO tasks (project_id, key, title, specification, state)
           VALUES (1, 'AX-001', 't', 's', 'ASSIGNED')"""
    )
    return 1, 1


def _insert_execution(
    conn: psycopg.Connection, status: str, lease: bool, ended: bool, task_id: int = 1
) -> None:
    conn.execute(
        """INSERT INTO executions (task_id, agent_id, attempt, status, lease_expires_at, ended_at)
           VALUES (%s, 1, 1, %s,
                   CASE WHEN %s THEN now() + interval '1 minute' END,
                   CASE WHEN %s THEN now() END)""",
        (task_id, status, lease, ended),
    )


def test_active_execution_with_lease_is_accepted(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    _insert_execution(conn, "STARTING", lease=True, ended=False)


def test_ended_execution_holding_a_lease_is_rejected(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    with pytest.raises(errors.CheckViolation, match="active_iff_leased"):
        _insert_execution(conn, "COMPLETED", lease=True, ended=True)


def test_active_execution_without_lease_is_rejected(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    with pytest.raises(errors.CheckViolation, match="active_iff_leased"):
        _insert_execution(conn, "RUNNING", lease=False, ended=False)


def test_terminal_execution_without_ended_at_is_rejected(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    with pytest.raises(errors.CheckViolation, match="active_iff_not_ended"):
        _insert_execution(conn, "FAILED", lease=False, ended=False)


def test_second_active_execution_for_same_task_is_rejected(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    _insert_execution(conn, "COMPLETED", lease=False, ended=True)  # history is fine
    _insert_execution(conn, "RUNNING", lease=True, ended=False)
    with pytest.raises(errors.UniqueViolation, match="one_active_execution_per_task"):
        _insert_execution(conn, "STARTING", lease=True, ended=False)


def test_unknown_task_state_is_rejected(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    with pytest.raises(errors.CheckViolation):
        conn.execute("UPDATE tasks SET state = 'SHIPPED' WHERE id = 1")


def test_invalid_agent_role_and_runtime_are_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        conn.execute("INSERT INTO agents (key, role, runtime) VALUES ('X-01', 'cto', 'codex')")
    with pytest.raises(errors.CheckViolation):
        conn.execute("INSERT INTO agents (key, role, runtime) VALUES ('X-01', 'developer', 'gpt')")


def test_task_cannot_depend_on_itself(
    conn: psycopg.Connection, task_and_agent: tuple[int, int]
) -> None:
    with pytest.raises(errors.CheckViolation):
        conn.execute("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (1, 1)")


def test_events_are_append_only(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO events (actor, type) VALUES ('human:test', 'TEST')")
    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute("UPDATE events SET type = 'TAMPERED'")
    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute("DELETE FROM events")
    assert conn.execute("SELECT type FROM events").fetchall() == [("TEST",)]


def test_migrate_accepts_dict_row_connections(db_url: str) -> None:
    """Regression: the app passes dict-row connections; migrate must not index rows by [0]."""
    from axiom.state.db import connect

    with connect(db_url) as c:
        assert migrate(c) == []
