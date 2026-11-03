"""T3: projects and the agent registry; every change writes an event."""

from pathlib import Path

import pytest

from axiom.domain.roles import Actor, Role, Runtime
from axiom.errors import AlreadyExists, InvalidInput, NotFound
from axiom.events.log import list_events
from axiom.registry.agents import (
    get_agent,
    list_agents,
    register_agent,
    seed_agents,
    set_enabled,
)
from axiom.registry.projects import create_project, get_project, list_projects
from axiom.registry.seed import DEFAULT_CONFIG
from axiom.state.db import Conn


def _event_types(db: Conn) -> list[str]:
    return [e["type"] for e in list_events(db)]


# ---- projects -----------------------------------------------------------------------------


def test_create_and_get_project(db: Conn, human: Actor) -> None:
    p = create_project(db, human, key="AX", name="Axiom", repo_url="https://example/axiom")
    assert get_project(db, "AX") == p
    assert list_projects(db) == [p]
    events = list_events(db)
    assert [(e["type"], e["project_id"], e["actor"]) for e in events] == [
        ("PROJECT_CREATED", p.id, "human:tester")
    ]


def test_duplicate_project_rejected_without_event(db: Conn, human: Actor) -> None:
    create_project(db, human, key="AX", name="Axiom")
    with pytest.raises(AlreadyExists):
        create_project(db, human, key="AX", name="Again")
    assert _event_types(db) == ["PROJECT_CREATED"]


@pytest.mark.parametrize("key", ["ax", "A", "AX-1", "TOOLONGKEY1", ""])
def test_invalid_project_key_rejected(db: Conn, human: Actor, key: str) -> None:
    with pytest.raises(InvalidInput):
        create_project(db, human, key=key, name="x")
    assert _event_types(db) == []


def test_unknown_project_not_found(db: Conn) -> None:
    with pytest.raises(NotFound):
        get_project(db, "NOPE")


# ---- agents -------------------------------------------------------------------------------


def test_register_and_get_agent(db: Conn, human: Actor) -> None:
    a = register_agent(
        db,
        human,
        key="DEV-01",
        role="developer",
        runtime="claude_code",
        model="m",
        max_concurrent=2,
    )
    assert (a.role, a.runtime, a.max_concurrent, a.enabled) == (
        Role.DEVELOPER, Runtime.CLAUDE_CODE, 2, True,
    )  # fmt: skip
    assert get_agent(db, "DEV-01") == a
    [event] = list_events(db)
    assert (event["type"], event["agent_id"]) == ("AGENT_REGISTERED", a.id)
    assert event["payload"]["role"] == "developer"


def test_duplicate_agent_rejected(db: Conn, human: Actor) -> None:
    register_agent(db, human, key="DEV-01", role="developer", runtime="claude_code")
    with pytest.raises(AlreadyExists):
        register_agent(db, human, key="DEV-01", role="qa", runtime="codex")
    assert _event_types(db) == ["AGENT_REGISTERED"]


@pytest.mark.parametrize(
    ("key", "role", "runtime", "max_concurrent"),
    [
        ("dev-01", "developer", "claude_code", 1),
        ("DEV-1", "developer", "claude_code", 1),
        ("DEV-01", "cto", "claude_code", 1),
        ("DEV-01", "developer", "gpt", 1),
        ("DEV-01", "developer", "codex", 0),
    ],
)
def test_invalid_agent_rejected(
    db: Conn, human: Actor, key: str, role: str, runtime: str, max_concurrent: int
) -> None:
    with pytest.raises(InvalidInput):
        register_agent(
            db, human, key=key, role=role, runtime=runtime, max_concurrent=max_concurrent
        )
    assert list_agents(db) == []
    assert _event_types(db) == []


def test_enable_disable_writes_events_only_on_change(db: Conn, human: Actor) -> None:
    register_agent(db, human, key="DEV-01", role="developer", runtime="claude_code")
    assert set_enabled(db, human, "DEV-01", False).enabled is False
    assert set_enabled(db, human, "DEV-01", False).enabled is False  # no-op
    assert set_enabled(db, human, "DEV-01", True).enabled is True
    assert _event_types(db) == ["AGENT_REGISTERED", "AGENT_DISABLED", "AGENT_ENABLED"]


def test_set_enabled_unknown_agent(db: Conn, human: Actor) -> None:
    with pytest.raises(NotFound):
        set_enabled(db, human, "DEV-99", False)


def test_list_agents_filters(db: Conn, human: Actor) -> None:
    register_agent(db, human, key="DEV-01", role="developer", runtime="claude_code")
    register_agent(db, human, key="DEV-02", role="developer", runtime="claude_code")
    register_agent(db, human, key="REV-01", role="code_reviewer", runtime="codex")
    set_enabled(db, human, "DEV-02", False)
    assert [a.key for a in list_agents(db, role="developer")] == ["DEV-01", "DEV-02"]
    assert [a.key for a in list_agents(db, enabled=True)] == ["DEV-01", "REV-01"]
    assert [a.key for a in list_agents(db, role="developer", enabled=False)] == ["DEV-02"]


def test_seed_registers_the_initial_organization_once(db: Conn, human: Actor) -> None:
    created = seed_agents(db, human, DEFAULT_CONFIG)
    assert [a.key for a in created] == [
        "EM-01", "ARCH-01", "DEV-01", "DEV-02", "QA-01", "SEC-01", "REV-01",
    ]  # fmt: skip
    assert {a.role for a in created} == set(Role)
    assert seed_agents(db, human, DEFAULT_CONFIG) == []  # idempotent
    assert _event_types(db).count("AGENT_REGISTERED") == 7


def test_seed_skips_existing_and_adds_new(db: Conn, human: Actor, tmp_path: Path) -> None:
    register_agent(db, human, key="DEV-01", role="developer", runtime="claude_code")
    cfg = tmp_path / "agents.toml"
    cfg.write_text(
        '[[agents]]\nkey = "DEV-01"\nrole = "developer"\nruntime = "claude_code"\n'
        '[[agents]]\nkey = "QA-01"\nrole = "qa"\nruntime = "codex"\n'
    )
    assert [a.key for a in seed_agents(db, human, cfg)] == ["QA-01"]
