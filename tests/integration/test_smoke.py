import psycopg


def test_database_is_reachable(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT 1").fetchone()
    assert row == (1,)


def test_connected_to_test_database(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT current_database()").fetchone()
    assert row == ("axiom_test",)
