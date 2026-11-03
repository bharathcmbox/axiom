"""Agent registry: the logical employees of the organization (role, runtime, model, limits)."""

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import errors

from axiom.domain.roles import Actor, Role, Runtime
from axiom.errors import AlreadyExists, InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.state.db import Conn

AGENT_KEY = re.compile(r"^[A-Z]+-[0-9]{2}$")


@dataclass(frozen=True)
class Agent:
    id: int
    key: str
    role: Role
    runtime: Runtime
    model: str | None
    instructions_path: str | None
    max_concurrent: int
    enabled: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Agent:
        fields = {f: row[f] for f in cls.__dataclass_fields__}
        return cls(**fields | {"role": Role(row["role"]), "runtime": Runtime(row["runtime"])})


def _validate(key: str, role: str, runtime: str, max_concurrent: int) -> tuple[Role, Runtime]:
    if not AGENT_KEY.match(key):
        raise InvalidInput(f"agent key must match {AGENT_KEY.pattern}: {key!r}")
    try:
        role_ = Role(role)
    except ValueError:
        raise InvalidInput(f"unknown role {role!r}; one of {[r.value for r in Role]}") from None
    try:
        runtime_ = Runtime(runtime)
    except ValueError:
        raise InvalidInput(
            f"unknown runtime {runtime!r}; one of {[r.value for r in Runtime]}"
        ) from None
    if max_concurrent < 1:
        raise InvalidInput("max_concurrent must be >= 1")
    return role_, runtime_


def register_agent(
    conn: Conn,
    actor: Actor,
    *,
    key: str,
    role: str,
    runtime: str,
    model: str | None = None,
    instructions_path: str | None = None,
    max_concurrent: int = 1,
) -> Agent:
    role_, runtime_ = _validate(key, role, runtime, max_concurrent)
    with conn.transaction():
        try:
            row = conn.execute(
                """INSERT INTO agents (key, role, runtime, model, instructions_path,
                                       max_concurrent)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (key, role_, runtime_, model, instructions_path, max_concurrent),
            ).fetchone()
        except errors.UniqueViolation as e:
            raise AlreadyExists(f"agent {key} already exists") from e
        assert row is not None
        agent = Agent.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="AGENT_REGISTERED",
            agent_id=agent.id,
            payload={"key": key, "role": role_, "runtime": runtime_, "model": model},
        )
    return agent


def get_agent(conn: Conn, key: str) -> Agent:
    row = conn.execute("SELECT * FROM agents WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise NotFound(f"agent {key} not found")
    return Agent.from_row(row)


def list_agents(conn: Conn, *, role: str | None = None, enabled: bool | None = None) -> list[Agent]:
    rows = conn.execute(
        """SELECT * FROM agents
           WHERE (%(role)s::text IS NULL OR role = %(role)s)
             AND (%(enabled)s::boolean IS NULL OR enabled = %(enabled)s)
           ORDER BY id""",
        {"role": role, "enabled": enabled},
    )
    return [Agent.from_row(r) for r in rows]


def set_enabled(conn: Conn, actor: Actor, key: str, enabled: bool) -> Agent:
    """Enable or disable an agent. A no-op (already in that state) writes no event."""
    with conn.transaction():
        row = conn.execute("SELECT * FROM agents WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"agent {key} not found")
        if row["enabled"] == enabled:
            return Agent.from_row(row)
        row = conn.execute(
            "UPDATE agents SET enabled = %s WHERE id = %s RETURNING *", (enabled, row["id"])
        ).fetchone()
        assert row is not None
        agent = Agent.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="AGENT_ENABLED" if enabled else "AGENT_DISABLED",
            agent_id=agent.id,
        )
    return agent


def seed_agents(conn: Conn, actor: Actor, config_path: Path) -> list[Agent]:
    """Register every agent in a TOML file that is not registered yet. Returns the new ones."""
    data = tomllib.loads(config_path.read_text())
    created = []
    existing = {a.key for a in list_agents(conn)}
    for spec in data.get("agents", []):
        if spec["key"] in existing:
            continue
        created.append(register_agent(conn, actor, **spec))
    return created
