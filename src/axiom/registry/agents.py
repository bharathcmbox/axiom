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
from axiom.registry.guards import require_not_agent
from axiom.state.db import Conn

AGENT_KEY = re.compile(r"^[A-Z]+-[0-9]{2}$")

# ADR-009: work is reviewed on a model other than the one that wrote it. Agents with no model run
# on their worker's default, so these stand in for them (worker/__main__.py, review_harness.py).
MODEL_LOCK_ID = 727_002
MAKERS = frozenset({Role.DEVELOPER, Role.QA})
REVIEWERS = frozenset({Role.CODE_REVIEWER, Role.SECURITY_REVIEWER})
DEFAULT_MODEL = {Role.DEVELOPER: "claude-opus-5-5", Role.QA: "claude-opus-5-5",
                 Role.CODE_REVIEWER: "claude-fable-5-1",
                 Role.SECURITY_REVIEWER: "claude-fable-5-1"}  # fmt: skip


def effective_model(role: Role, model: str | None) -> str | None:
    return model or DEFAULT_MODEL.get(role)


def _check_reviewed_on_another_model(conn: Conn, key: str, role: Role, model: str | None) -> None:
    """Refuse a reviewer on a model that writes code, or a code writer on a reviewer's model."""
    if role not in MAKERS | REVIEWERS:
        return
    # Two changes at once could each pass against the other's old model: serialize them.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (MODEL_LOCK_ID,))
    mine = effective_model(role, model)
    others = MAKERS if role in REVIEWERS else REVIEWERS
    for row in conn.execute("SELECT key, role, model FROM agents WHERE key <> %s", (key,)):
        other = Role(row["role"])
        if other in others and effective_model(other, row["model"]) == mine:
            raise InvalidInput(
                f"{key} ({role}) can't run on {mine}: {row['key']} ({other}) does, and work "
                "must be reviewed on a different model from the one that wrote it (ADR-009)"
            )


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
    require_not_agent(actor)
    role_, runtime_ = _validate(key, role, runtime, max_concurrent)
    with conn.transaction():
        _check_reviewed_on_another_model(conn, key, role_, model)
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
    require_not_agent(actor)
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


def set_model(conn: Conn, actor: Actor, key: str, model: str | None) -> Agent:
    """Change which model an agent runs on (humans only), e.g. reviewers on a different model."""
    require_not_agent(actor)
    if model is not None and not re.match(r"^[a-z0-9][a-z0-9.\-]{2,63}$", model):
        raise InvalidInput(f"not a model id: {model!r}")
    with conn.transaction():
        row = conn.execute("SELECT * FROM agents WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"agent {key} not found")
        old = row["model"]
        if old == model:
            return Agent.from_row(row)
        _check_reviewed_on_another_model(conn, key, Role(row["role"]), model)
        row = conn.execute(
            "UPDATE agents SET model = %s WHERE id = %s RETURNING *", (model, row["id"])
        ).fetchone()
        assert row is not None
        agent = Agent.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="AGENT_MODEL_CHANGED",
            agent_id=agent.id,
            payload={"from": old, "to": model},
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
