"""T8: state lives only in Postgres. Tearing down the app (pool, reaper, everything in memory)
and building a new one preserves every task, execution and event; executions orphaned while
the service was down are timed out by the startup sweep."""

from typing import Any

from fastapi.testclient import TestClient

from axiom.api.app import create_app
from axiom.config import Settings
from axiom.state.db import Conn
from tests.conftest import bearer
from tests.integration.helpers import expire_lease

H = bearer("human:bharath")
DEV1 = bearer("agent:DEV-01")
DEV2 = bearer("agent:DEV-02")


def _app(db_url: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=db_url), start_reaper=False), headers=H)


def _snapshot(c: TestClient) -> dict[str, Any]:
    tasks = c.get("/tasks", headers=H).json()
    return {
        "tasks": tasks,
        "executions": {
            t["key"]: c.get(f"/tasks/{t['key']}/executions", headers=H).json() for t in tasks
        },
        "events": c.get("/events", params={"limit": 1000}, headers=H).json(),
    }


def _populate(c: TestClient) -> dict[str, str]:
    """One task in each of several states. Returns state -> task key."""

    def task(**kw: Any) -> str:
        body = {"title": "t", "specification": "s"} | kw
        return str(c.post("/projects/AX/tasks", json=body, headers=H).json()["key"])

    def cmd(key: str, command: str, headers: dict[str, str], **body: Any) -> None:
        r = c.post(f"/tasks/{key}/commands/{command}", json=body, headers=headers)
        assert r.status_code == 200, r.text

    keys = {"READY": task()}
    keys["PENDING"] = task(dependencies=[keys["READY"]])
    for state, steps in {
        "DONE": ["assign", "start", "submit", "approve", "merge"],
        "AWAITING_MERGE": ["assign", "start", "submit", "approve"],
        "IN_REVIEW": ["assign", "start", "submit"],
        "WAITING_FOR_HUMAN": ["assign", "start", "ask_human"],
        "CANCELLED": ["cancel"],
    }.items():
        keys[state] = key = task()
        for step in steps:
            actor = DEV2 if step in ("start", "submit", "ask_human") else H
            cmd(key, step, actor, **({"agent_key": "DEV-02"} if step == "assign" else {}))
    keys["FAILED"] = key = task(max_retries=0)
    cmd(key, "assign", H, agent_key="DEV-02")
    cmd(key, "start", DEV2)
    cmd(key, "fail", DEV2, payload={"error": "boom"})
    keys["IN_PROGRESS"] = key = task(priority="P0")
    cmd(key, "assign", H, agent_key="DEV-01")
    cmd(key, "start", DEV1)
    return keys


def test_restart_preserves_all_state(api_tokens: Conn, db_url: str) -> None:
    with _app(db_url) as first:
        keys = _populate(first)
        before = _snapshot(first)
    assert {t["state"] for t in before["tasks"]} == set(keys)  # 9 different states

    with _app(db_url) as second:  # brand-new pool and app; nothing carried over in memory
        assert _snapshot(second) == before
        assert second.get("/invariants", headers=H).json() == {"violations": []}
        # ...and work continues where it left off
        r = second.post(f"/tasks/{keys['IN_PROGRESS']}/commands/submit", json={}, headers=DEV1)
        assert r.json()["state"] == "IN_REVIEW"


def test_startup_times_out_executions_orphaned_while_down(api_tokens: Conn, db_url: str) -> None:
    with _app(db_url) as first:
        keys = _populate(first)
    expire_lease(api_tokens, keys["IN_PROGRESS"])  # the worker died while Axiom was down

    with _app(db_url) as second:
        task = second.get(f"/tasks/{keys['IN_PROGRESS']}", headers=H).json()
        assert (task["state"], task["attempt_count"]) == ("READY", 1)
        last = second.get(f"/tasks/{keys['IN_PROGRESS']}/events", headers=H).json()[-1]
        assert (last["type"], last["actor"]) == ("EXECUTION_TIMED_OUT", "system:reaper")
