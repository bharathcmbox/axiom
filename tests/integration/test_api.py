"""T6: the REST API drives everything; domain errors map to the right HTTP status."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from axiom.api.app import create_app
from axiom.config import Settings
from axiom.state.db import Conn
from tests.conftest import bearer
from tests.integration.review_helpers import with_checklist

H = bearer("human:bharath")
DEV1 = bearer("agent:DEV-01")
DEV2 = bearer("agent:DEV-02")


@pytest.fixture
def client(api_tokens: Conn, db_url: str) -> Iterator[TestClient]:
    """API over the test DB, with project AX and the 7 seeded agents already present."""
    app = create_app(Settings(database_url=db_url), start_reaper=False)
    with TestClient(app, headers=H) as c:  # human token by default; requests may override
        yield c


@pytest.fixture
def anon(api_tokens: Conn, db_url: str) -> Iterator[TestClient]:
    """A client that sends no token."""
    app = create_app(Settings(database_url=db_url), start_reaper=False)
    with TestClient(app) as c:
        yield c


def _task(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {"title": "Implement POST /alerts", "specification": "spec"} | overrides
    r = client.post("/projects/AX/tasks", json=body, headers=H)
    assert r.status_code == 201, r.text
    return dict(r.json())


def _cmd(client: TestClient, key: str, command: str, headers: dict[str, str], **body: Any) -> Any:
    return client.post(f"/tasks/{key}/commands/{command}", json=body, headers=headers)


def test_healthz_and_invariants(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "database": "ok"}
    assert client.get("/invariants").json() == {"violations": []}


def test_openapi_lists_the_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/claims", "/tasks/{key}/commands/{command}", "/tasks/{key}/heartbeat"} <= set(paths)


# ---- projects and agents --------------------------------------------------------------------


def test_projects(client: TestClient) -> None:
    r = client.post("/projects", json={"key": "SOC", "name": "Agentic SOC"}, headers=H)
    assert r.status_code == 201 and r.json()["key"] == "SOC"
    assert client.post("/projects", json={"key": "SOC", "name": "x"}, headers=H).status_code == 409
    r = client.post("/projects", json={"key": "bad key", "name": "x"}, headers=H)
    assert (r.status_code, r.json()["error"]) == (422, "invalid_input")
    assert [p["key"] for p in client.get("/projects").json()] == ["AX", "SOC"]
    assert client.get("/projects/NOPE").status_code == 404


def test_agents(client: TestClient) -> None:
    assert len(client.get("/agents").json()) == 7
    assert [a["key"] for a in client.get("/agents", params={"role": "developer"}).json()] == [
        "DEV-01", "DEV-02",
    ]  # fmt: skip
    r = client.post(
        "/agents", json={"key": "REV-02", "role": "code_reviewer", "runtime": "codex"}, headers=H
    )
    assert r.status_code == 201 and r.json()["runtime"] == "codex"
    bad = {"key": "X-01", "role": "cto", "runtime": "codex"}
    assert client.post("/agents", json=bad, headers=H).status_code == 422
    assert client.post("/agents/DEV-02/disable", headers=H).json()["enabled"] is False
    assert client.post("/agents/DEV-02/enable", headers=H).json()["enabled"] is True
    assert client.get("/agents/NOPE-01").status_code == 404


# ---- the whole lifecycle over HTTP ----------------------------------------------------------


def test_full_lifecycle_over_http(client: TestClient) -> None:
    dep = _task(client, title="dependency", priority="P1")
    task = _task(client, dependencies=[dep["key"]], acceptance_criteria=["returns 201"])
    assert (dep["state"], task["state"]) == ("READY", "PENDING")

    r = client.post("/claims", headers=DEV1)
    assert r.status_code == 200 and r.json()["key"] == dep["key"]
    assert r.json()["state"] == "ASSIGNED"
    assert client.post("/claims", headers=DEV2).status_code == 204  # nothing else READY

    key = dep["key"]
    assert _cmd(client, key, "start", DEV1).json()["state"] == "IN_PROGRESS"
    r = client.post(f"/tasks/{key}/heartbeat", json={"lease_seconds": 120}, headers=DEV1)
    assert r.status_code == 200 and r.json()["task_key"] == key
    result = {"summary": "done", "files_changed": ["src/a.py"], "cost_usd": 1.84}
    assert _cmd(client, key, "submit", DEV1, payload=result).json()["state"] == "IN_REVIEW"
    assert _cmd(client, key, "approve", H).json()["state"] == "AWAITING_MERGE"
    assert _cmd(client, key, "merge", H).json()["state"] == "DONE"

    assert client.get(f"/tasks/{task['key']}").json()["state"] == "READY"  # promoted
    executions = client.get(f"/tasks/{key}/executions").json()
    assert [(e["status"], e["result"]) for e in executions] == [("COMPLETED", result)]
    events = client.get(f"/tasks/{key}/events").json()
    assert [(e["type"], e["from_state"], e["to_state"]) for e in events] == [
        ("TASK_CREATED", None, "PENDING"),
        ("TASK_READY", "PENDING", "READY"),
        ("TASK_ASSIGNED", "READY", "ASSIGNED"),
        ("AGENT_STARTED", "ASSIGNED", "IN_PROGRESS"),
        ("TASK_SUBMITTED", "IN_PROGRESS", "IN_REVIEW"),
        ("REVIEW_REQUESTED", None, None),  # code review   (Phase 3)
        ("REVIEW_REQUESTED", None, None),  # security review
        ("REVIEW_APPROVED", "IN_REVIEW", "AWAITING_MERGE"),  # a human override...
        ("REVIEW_SUPERSEDED", None, None),  # ...closes the open reviews
        ("REVIEW_SUPERSEDED", None, None),
        ("TASK_DONE", "AWAITING_MERGE", "DONE"),
    ]
    assert events[-1]["actor"] == "human:bharath"
    all_events = client.get("/events", params={"after_id": events[0]["id"], "limit": 3}).json()
    assert [e["id"] for e in all_events] == [events[0]["id"] + i for i in (1, 2, 3)]


def test_rework_and_human_input_over_http(client: TestClient) -> None:
    key = _task(client)["key"]
    _cmd(client, key, "assign", H, agent_key="DEV-02")
    _cmd(client, key, "start", DEV2)
    r = _cmd(client, key, "ask_human", DEV2, payload={"question": "REST or gRPC?"})
    assert r.json()["state"] == "WAITING_FOR_HUMAN"
    assert _cmd(client, key, "answer", H, payload={"answer": "REST"}).json()["state"] == "READY"
    client.post("/claims", headers=DEV1)
    _cmd(client, key, "start", DEV1)
    _cmd(client, key, "submit", DEV1)
    r = _cmd(client, key, "request_changes", H, payload={"findings": [{"severity": "high"}]})
    assert (r.json()["state"], r.json()["review_rounds"]) == ("READY", 1)


# ---- errors ---------------------------------------------------------------------------------


def test_illegal_command_is_409_with_allowed_commands(client: TestClient) -> None:
    key = _task(client)["key"]
    r = _cmd(client, key, "merge", H)
    assert r.status_code == 409
    assert r.json() == {
        "error": "illegal_transition",
        "detail": r.json()["detail"],
        "state": "READY",
        "command": "merge",
        "allowed": ["claim", "assign", "cancel"],
    }
    assert client.get(f"/tasks/{key}").json()["state"] == "READY"
    assert len(client.get(f"/tasks/{key}/events").json()) == 2  # created + ready only


def test_agents_cannot_merge(client: TestClient) -> None:
    key = _task(client)["key"]
    r = _cmd(client, key, "cancel", DEV1)
    assert (r.status_code, r.json()["error"]) == (403, "actor_not_permitted")


def test_every_route_but_healthz_requires_a_token(anon: TestClient) -> None:
    assert anon.get("/healthz").status_code == 200
    for method, path in [
        ("GET", "/tasks"), ("GET", "/agents"), ("GET", "/events"), ("GET", "/invariants"),
        ("POST", "/projects/AX/tasks"), ("POST", "/claims"),
        ("POST", "/tasks/AX-001/commands/cancel"), ("POST", "/tasks/AX-001/heartbeat"),
    ]:  # fmt: skip
        r = anon.request(method, path)
        assert r.status_code == 401, (method, path)
        assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer axm_not-a-real-token", "Bearer ", "Basic dXNlcjpwYXNz", "axm_test-human-bharath"],
)
def test_bad_tokens_are_401(anon: TestClient, header: str) -> None:
    assert anon.get("/tasks", headers={"Authorization": header}).status_code == 401


def test_revoked_token_is_401(client: TestClient, api_tokens: Conn) -> None:
    api_tokens.execute("UPDATE api_tokens SET revoked_at = now() WHERE actor = 'agent:DEV-01'")
    assert client.post("/claims", headers=DEV1).status_code == 401


def test_actor_comes_from_the_token_not_the_client(client: TestClient) -> None:
    key = _task(client)["key"]
    # A spoofed X-Axiom-Actor header is ignored: DEV-01's token still acts as DEV-01.
    r = _cmd(client, key, "cancel", DEV1 | {"X-Axiom-Actor": "human:bharath"})
    assert (r.status_code, r.json()["error"]) == (403, "actor_not_permitted")
    events = client.get(f"/tasks/{key}/events").json()
    assert {e["actor"] for e in events} <= {"human:bharath", "system:dependencies"}


def test_agents_cannot_change_the_registry(client: TestClient) -> None:
    r = client.post("/projects", json={"key": "EVIL", "name": "x"}, headers=DEV1)
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    agent = {"key": "DEV-09", "role": "developer", "runtime": "claude_code"}
    assert client.post("/agents", json=agent, headers=DEV1).status_code == 403
    assert client.post("/agents/DEV-02/disable", headers=DEV1).status_code == 403


def test_unknown_and_internal_commands_rejected(client: TestClient) -> None:
    key = _task(client)["key"]
    for command in ("deploy", "claim", "promote", "time_out"):
        assert _cmd(client, key, command, H).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"specification": "s"},
        {"title": "", "specification": "s"},
        {"title": "t", "specification": "s", "priority": "urgent"},
        {"title": "t", "specification": "s", "max_retries": -1},
        {"title": "t", "specification": "s", "assigned_role": "cto"},
        {"title": "t", "specification": "s", "dependencies": ["AX-999"]},
    ],
)
def test_malformed_task_is_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/projects/AX/tasks", json=body, headers=H).status_code == 422
    assert client.get("/tasks").json() == []


def test_not_found(client: TestClient) -> None:
    assert client.get("/tasks/AX-999").status_code == 404
    assert _cmd(client, "AX-999", "cancel", H).status_code == 404
    assert client.post("/projects/NOPE/tasks", json={"title": "t", "specification": "s"},
                       headers=H).status_code == 404  # fmt: skip


def test_lease_and_ownership_errors(client: TestClient) -> None:
    key = _task(client)["key"]
    client.post("/claims", headers=DEV1)
    r = client.post(f"/tasks/{key}/heartbeat", headers=DEV2)
    assert (r.status_code, r.json()["error"]) == (403, "not_execution_owner")
    r = _cmd(client, key, "start", DEV2)
    assert (r.status_code, r.json()["error"]) == (403, "not_execution_owner")
    _task(client)  # another READY task, but DEV-01 is already at capacity
    r = client.post("/claims", headers=DEV1)
    assert (r.status_code, r.json()["error"]) == (409, "assignment_rejected")


def test_task_filters(client: TestClient) -> None:
    a = _task(client)
    _task(client, dependencies=[a["key"]])
    assert [t["state"] for t in client.get("/tasks").json()] == ["READY", "PENDING"]
    assert [t["key"] for t in client.get("/tasks", params={"state": "PENDING"}).json()] == [
        "AX-002"
    ]
    assert client.get("/tasks", params={"state": "BOGUS"}).status_code == 422
    assert len(client.get("/tasks", params={"project": "AX"}).json()) == 2


def test_budget_fields_and_route(client: TestClient) -> None:
    task = _task(client, budget_usd=2.5, allowed_paths=["CLAUDE.md"])
    assert (task["budget_usd"], task["budget_spent_usd"], task["run_budget_usd"]) == (
        "2.50", "0", "2.50",
    )  # fmt: skip
    assert task["allowed_paths"] == ["CLAUDE.md"]
    r = client.post(f"/tasks/{task['key']}/budget", json={"budget_usd": 5}, headers=H)
    assert r.status_code == 200 and r.json()["budget_usd"] == "5.00"
    assert client.post(f"/tasks/{task['key']}/budget", json={"budget_usd": 5},
                       headers=DEV1).status_code == 403  # fmt: skip
    r = client.post(
        "/projects",
        json={"key": "SOC", "name": "soc", "test_command": "cd agent && uv run pytest -q"},
    )
    assert r.json()["default_branch"] == "main"


def test_review_flow_over_http(client: TestClient) -> None:
    rev, sec = bearer("agent:REV-01"), bearer("agent:SEC-01")
    key = _task(client)["key"]
    assert client.post("/reviews/claim", headers=rev).status_code == 204  # nothing to review
    client.post("/claims", headers=DEV1)
    _cmd(client, key, "start", DEV1)
    _cmd(client, key, "submit", DEV1, payload={"summary": "done", "commit": "abc123"})

    assert client.post("/reviews/claim", headers=DEV1).status_code == 403  # not a reviewer
    code = client.post("/reviews/claim", headers=rev).json()
    assert (code["role"], code["task"]["key"], code["head_sha"]) == ("code_reviewer", key, "abc123")
    security = client.post("/reviews/claim", headers=sec).json()

    passed = {"verdict": "PASS", "summary": "fine", "findings": []}
    r = client.post(f"/reviews/{code['review_id']}/submit", headers=sec, json={"contract": passed})
    assert (r.status_code, r.json()["error"]) == (403, "not_execution_owner")
    bad = client.post(f"/reviews/{code['review_id']}/submit", headers=rev,
                      json={"contract": {"verdict": "OK"}})  # fmt: skip
    assert bad.status_code == 422
    r = client.post(f"/reviews/{code['review_id']}/submit", headers=rev,
                    json={"contract": with_checklist(passed, "code_reviewer"),
                          "cost": {"usd": "0.30"}})  # fmt: skip
    assert r.json() == {"review_id": code["review_id"], "decision": "wait"}
    r = client.post(f"/reviews/{security['review_id']}/submit", headers=sec,
                    json={"contract": with_checklist(passed, "security_reviewer")})  # fmt: skip
    assert r.json()["decision"] == "approve"
    assert client.get(f"/tasks/{key}").json()["state"] == "AWAITING_MERGE"
    reviews = client.get(f"/tasks/{key}/reviews").json()
    assert [(x["role"], x["status"], x["reviewer_key"]) for x in reviews] == [
        ("code_reviewer", "PASS", "REV-01"), ("security_reviewer", "PASS", "SEC-01"),
    ]  # fmt: skip


def test_agents_cannot_approve_over_http(client: TestClient) -> None:
    key = _task(client)["key"]
    client.post("/claims", headers=DEV1)
    _cmd(client, key, "start", DEV1)
    _cmd(client, key, "submit", DEV1)
    r = _cmd(client, key, "approve", DEV1)
    assert (r.status_code, r.json()["error"]) == (403, "actor_not_permitted")


def test_set_agent_model_over_http(client: TestClient) -> None:
    r = client.post("/agents/REV-01/model", json={"model": "claude-fable-5-1"})
    assert r.status_code == 200 and r.json()["model"] == "claude-fable-5-1"
    assert client.post("/agents/REV-01/model", json={"model": "x"}, headers=DEV1).status_code == 403


def test_require_ci_toggle(client: TestClient) -> None:
    r = client.post("/projects/AX/require-ci", json={"require_ci": True})
    assert r.status_code == 200 and r.json()["require_ci"] is True
    assert client.post("/projects/AX/require-ci", json={"require_ci": False},
                       headers=DEV1).status_code == 403  # fmt: skip
    assert client.post("/projects/NOPE/require-ci", json={"require_ci": True}).status_code == 404
    types = [e["type"] for e in client.get("/events", params={"limit": 1000}).json()]
    assert types.count("PROJECT_CI_GATE_CHANGED") == 1


def test_metrics_over_http(client: TestClient) -> None:
    key = _task(client)["key"]
    r = client.get("/metrics").json()
    assert r["summary"]["tasks"] == 1 and r["tasks"][0]["key"] == key
    assert client.post(f"/tasks/{key}/tags", json={"tags": ["demo"]}).json() == {"tags": ["demo"]}
    assert client.get("/metrics").json()["summary"]["tasks"] == 0
    assert client.get("/metrics", params={"include_demo": True}).json()["summary"]["tasks"] == 1
    defect = {"description": "broke", "severity": "high"}
    assert client.post(f"/tasks/{key}/defects", json=defect).status_code == 201
    assert client.post(f"/tasks/{key}/defects", json=defect, headers=DEV1).status_code == 403
    base = {"project_key": "AX", "title": "by hand", "minutes": 30, "cost_usd": "1.20"}
    assert client.post("/baselines", json=base).status_code == 201
    assert client.get("/metrics").json()["baselines"][0]["payload"]["title"] == "by hand"
