"""Cross-table invariants that SQL constraints cannot express.

`check_invariants` returns human-readable violations (empty = healthy). It runs after every
integration test and once at application startup.
"""

from collections import defaultdict

from axiom.config import get_settings
from axiom.domain.task_machine import EXECUTING_STATES, TaskState
from axiom.state.db import Conn


def check_invariants(conn: Conn) -> list[str]:
    violations: list[str] = []
    violations += _executing_iff_one_active_execution(conn)
    violations += _event_log_replays_to_current_state(conn)
    violations += _dependencies_consistent(conn)
    violations += _retry_counters(conn)
    violations += _agent_assignments(conn)
    return violations


def _executing_iff_one_active_execution(conn: Conn) -> list[str]:
    rows = conn.execute(
        """SELECT t.key, t.state, count(e.id) AS active
           FROM tasks t
           LEFT JOIN executions e ON e.task_id = t.id AND e.status IN ('STARTING', 'RUNNING')
           GROUP BY t.id ORDER BY t.id"""
    )
    out = []
    for r in rows:
        executing = r["state"] in EXECUTING_STATES
        if r["active"] > 1 or executing != (r["active"] == 1):
            out.append(f"{r['key']}: state {r['state']} with {r['active']} active executions")
    return out


def _event_log_replays_to_current_state(conn: Conn) -> list[str]:
    """Every task's transition events chain from creation to its current state."""
    chains: dict[int, list[tuple[str | None, str]]] = defaultdict(list)
    for e in conn.execute(
        """SELECT task_id, from_state, to_state FROM events
           WHERE task_id IS NOT NULL AND to_state IS NOT NULL ORDER BY id"""
    ):
        chains[e["task_id"]].append((e["from_state"], e["to_state"]))
    out = []
    for t in conn.execute("SELECT id, key, state FROM tasks ORDER BY id"):
        current: str | None = None
        for from_state, to_state in chains.get(t["id"], []):
            if from_state != current:
                out.append(f"{t['key']}: event from {from_state} but task was in {current}")
            current = to_state
        if current != t["state"]:
            out.append(f"{t['key']}: events end at {current} but task is {t['state']}")
    return out


def _dependencies_consistent(conn: Conn) -> list[str]:
    out = []
    rows = conn.execute(
        """SELECT t.key, t.state, t.project_id,
                  coalesce(bool_and(d.state = 'DONE'), true) AS deps_done,
                  coalesce(bool_and(d.project_id = t.project_id), true) AS same_project
           FROM tasks t
           LEFT JOIN task_dependencies td ON td.task_id = t.id
           LEFT JOIN tasks d ON d.id = td.depends_on_id
           GROUP BY t.id ORDER BY t.id"""
    )
    for r in rows:
        if not r["same_project"]:
            out.append(f"{r['key']}: depends on a task in another project")
        if r["state"] == TaskState.PENDING and r["deps_done"]:
            out.append(f"{r['key']}: PENDING although all dependencies are DONE")
        if r["state"] not in (TaskState.PENDING, TaskState.CANCELLED) and not r["deps_done"]:
            out.append(f"{r['key']}: {r['state']} although a dependency is not DONE")
    return out


def _retry_counters(conn: Conn) -> list[str]:
    out = []
    for r in conn.execute("SELECT key, state, attempt_count, max_retries FROM tasks"):
        if r["attempt_count"] > r["max_retries"] + 1:
            out.append(f"{r['key']}: attempt_count {r['attempt_count']} > max_retries + 1")
        if r["state"] == TaskState.FAILED and r["attempt_count"] <= r["max_retries"]:
            out.append(f"{r['key']}: FAILED with retries left")
    return out


def _agent_assignments(conn: Conn) -> list[str]:
    out = []
    for r in conn.execute(
        """SELECT a.key, a.max_concurrent, count(e.id) AS active
           FROM agents a JOIN executions e ON e.agent_id = a.id
           WHERE e.status IN ('STARTING', 'RUNNING')
           GROUP BY a.id HAVING count(e.id) > a.max_concurrent"""
    ):
        out.append(f"agent {r['key']}: {r['active']} active > max_concurrent {r['max_concurrent']}")
    for r in conn.execute(
        """SELECT t.key, a.key AS agent, a.role, t.assigned_role
           FROM executions e JOIN tasks t ON t.id = e.task_id JOIN agents a ON a.id = e.agent_id
           WHERE e.status IN ('STARTING', 'RUNNING') AND a.role <> t.assigned_role"""
    ):
        out.append(
            f"{r['key']}: executed by {r['agent']} ({r['role']}), needs {r['assigned_role']}"
        )
    cap = get_settings().max_concurrent_developers
    row = conn.execute(
        """SELECT count(*) AS n FROM executions e JOIN agents a ON a.id = e.agent_id
           WHERE e.status IN ('STARTING', 'RUNNING') AND a.role = 'developer'"""
    ).fetchone()
    if row is not None and row["n"] > cap:
        out.append(f"{row['n']} active developer executions > global cap {cap}")
    return out
