"""Projects: a product specification and repository that Axiom delivers work for."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from axiom.domain.roles import Actor
from axiom.errors import AlreadyExists, InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.state.db import Conn

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


@dataclass(frozen=True)
class Project:
    id: int
    key: str
    name: str
    repo_url: str | None
    specification: str | None
    settings: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Project:
        return cls(**{f: row[f] for f in cls.__dataclass_fields__})


def create_project(
    conn: Conn,
    actor: Actor,
    *,
    key: str,
    name: str,
    repo_url: str | None = None,
    specification: str | None = None,
    settings: dict[str, Any] | None = None,
) -> Project:
    if not PROJECT_KEY.match(key):
        raise InvalidInput(f"project key must match {PROJECT_KEY.pattern}: {key!r}")
    if not name.strip():
        raise InvalidInput("project name must not be empty")
    with conn.transaction():
        try:
            row = conn.execute(
                """INSERT INTO projects (key, name, repo_url, specification, settings)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (key, name, repo_url, specification, Jsonb(settings or {})),
            ).fetchone()
        except errors.UniqueViolation as e:
            raise AlreadyExists(f"project {key} already exists") from e
        assert row is not None
        project = Project.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="PROJECT_CREATED",
            project_id=project.id,
            payload={"key": key, "name": name},
        )
    return project


def get_project(conn: Conn, key: str) -> Project:
    row = conn.execute("SELECT * FROM projects WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise NotFound(f"project {key} not found")
    return Project.from_row(row)


def list_projects(conn: Conn) -> list[Project]:
    return [Project.from_row(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]
