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
from axiom.paths import is_secret, normalize
from axiom.registry.guards import require_not_agent
from axiom.state.db import Conn

PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


SANDBOX_PYTHONS = ("3.12", "3.13", "3.14")  # installed in docker/sandbox/Dockerfile


@dataclass(frozen=True)
class Project:
    id: int
    key: str
    name: str
    repo_url: str | None
    specification: str | None
    settings: dict[str, Any]
    default_branch: str
    test_command: str | None
    require_ci: bool
    python_version: str | None
    test_paths: list[str]
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
    default_branch: str = "main",
    test_command: str | None = None,
    require_ci: bool = False,
) -> Project:
    require_not_agent(actor)
    if not PROJECT_KEY.match(key):
        raise InvalidInput(f"project key must match {PROJECT_KEY.pattern}: {key!r}")
    if not name.strip():
        raise InvalidInput("project name must not be empty")
    with conn.transaction():
        try:
            row = conn.execute(
                """INSERT INTO projects (key, name, repo_url, specification, settings,
                                        default_branch, test_command, require_ci)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    key,
                    name,
                    repo_url,
                    specification,
                    Jsonb(settings or {}),
                    default_branch,
                    test_command,
                    require_ci,
                ),
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


def set_python_version(conn: Conn, actor: Actor, key: str, version: str | None) -> Project:
    """Which Python uv uses in this project's sandboxes, to match its CI. None leaves it to uv,
    which prefers the image's newest managed Python (3.13), not the system's 3.14. Set it."""
    require_not_agent(actor)
    if version is not None and version not in SANDBOX_PYTHONS:
        raise InvalidInput(f"python_version must be one of {', '.join(SANDBOX_PYTHONS)}")
    with conn.transaction():
        row = conn.execute("SELECT * FROM projects WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"project {key} not found")
        if row["python_version"] == version:
            return Project.from_row(row)
        row = conn.execute(
            "UPDATE projects SET python_version = %s WHERE id = %s RETURNING *",
            (version, row["id"]),
        ).fetchone()
        assert row is not None
        project = Project.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="PROJECT_PYTHON_CHANGED",
            project_id=project.id,
            payload={"python_version": version},
        )
    return project


def set_test_paths(conn: Conn, actor: Actor, key: str, test_paths: list[str]) -> Project:
    """What a QA task may touch in this project. No test paths: plans can't include QA tasks."""
    require_not_agent(actor)
    clean = []
    for path in test_paths:
        stem = path.split("*", 1)[0].strip("/")
        if normalize(path) is None or ".." in path.split("/") or not stem or is_secret(path):
            raise InvalidInput(f"test path {path!r} must be a directory or file in the repository")
        clean.append(path)
    with conn.transaction():
        row = conn.execute("SELECT * FROM projects WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"project {key} not found")
        if row["test_paths"] == clean:
            return Project.from_row(row)
        row = conn.execute(
            "UPDATE projects SET test_paths = %s WHERE id = %s RETURNING *",
            (Jsonb(clean), row["id"]),
        ).fetchone()
        assert row is not None
        project = Project.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="PROJECT_TEST_PATHS_CHANGED",
            project_id=project.id,
            payload={"test_paths": clean},
        )
    return project


def set_require_ci(conn: Conn, actor: Actor, key: str, require_ci: bool) -> Project:
    """Turn the CI gate on or off for a project. A no-op writes no event."""
    require_not_agent(actor)
    with conn.transaction():
        row = conn.execute("SELECT * FROM projects WHERE key = %s FOR UPDATE", (key,)).fetchone()
        if row is None:
            raise NotFound(f"project {key} not found")
        if row["require_ci"] == require_ci:
            return Project.from_row(row)
        row = conn.execute(
            "UPDATE projects SET require_ci = %s WHERE id = %s RETURNING *", (require_ci, row["id"])
        ).fetchone()
        assert row is not None
        project = Project.from_row(row)
        append_event(
            conn,
            actor=actor,
            type="PROJECT_CI_GATE_CHANGED",
            project_id=project.id,
            payload={"require_ci": require_ci},
        )
    return project


def get_project(conn: Conn, key: str) -> Project:
    row = conn.execute("SELECT * FROM projects WHERE key = %s", (key,)).fetchone()
    if row is None:
        raise NotFound(f"project {key} not found")
    return Project.from_row(row)


def list_projects(conn: Conn) -> list[Project]:
    return [Project.from_row(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]
