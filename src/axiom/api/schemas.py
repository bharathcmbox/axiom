"""HTTP request/response models (the task contract lives here as TaskIn)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from axiom.domain.roles import Role, Runtime
from axiom.domain.task_machine import TaskState


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectIn(BaseModel):
    key: str = Field(examples=["AX"])
    name: str
    repo_url: str | None = None
    specification: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectOut(_Out):
    id: int
    key: str
    name: str
    repo_url: str | None
    specification: str | None
    settings: dict[str, Any]
    created_at: datetime


class AgentIn(BaseModel):
    key: str = Field(examples=["DEV-03"])
    role: Role
    runtime: Runtime
    model: str | None = None
    instructions_path: str | None = None
    max_concurrent: int = Field(default=1, ge=1)


class AgentOut(_Out):
    id: int
    key: str
    role: Role
    runtime: Runtime
    model: str | None
    instructions_path: str | None
    max_concurrent: int
    enabled: bool
    created_at: datetime


class TaskIn(BaseModel):
    """The task contract written by the planner (or a human) and approved by a human."""

    title: str = Field(min_length=1)
    specification: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    assigned_role: Role = Role.DEVELOPER
    priority: str = Field(default="P2", pattern=r"^P[0-3]$")
    max_retries: int = Field(default=2, ge=0)
    budget_usd: Decimal | None = Field(default=None, ge=0)
    dependencies: list[str] = Field(default_factory=list)


class TaskOut(_Out):
    id: int
    key: str
    project_id: int
    title: str
    specification: str
    acceptance_criteria: list[str]
    assigned_role: Role
    priority: str
    state: TaskState
    attempt_count: int
    max_retries: int
    review_rounds: int
    budget_usd: Decimal | None
    branch: str | None
    pull_request_url: str | None
    dependencies: list[str]
    created_at: datetime
    updated_at: datetime


class CommandIn(BaseModel):
    agent_key: str | None = Field(default=None, description="Required for `assign`.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Result contract, question, answer, findings or error, depending on command.",
    )
    lease_seconds: int | None = Field(default=None, ge=1)


class ClaimIn(BaseModel):
    project_key: str | None = None
    lease_seconds: int | None = Field(default=None, ge=1)


class HeartbeatIn(BaseModel):
    lease_seconds: int | None = Field(default=None, ge=1)


class HeartbeatOut(BaseModel):
    task_key: str
    lease_expires_at: datetime


class ExecutionOut(BaseModel):
    id: int
    task_id: int
    agent_id: int
    attempt: int
    status: str
    lease_expires_at: datetime | None
    started_at: datetime
    ended_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    cost_usd: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    transcript_uri: str | None


class EventOut(BaseModel):
    id: int
    occurred_at: datetime
    actor: str
    type: str
    project_id: int | None
    task_id: int | None
    execution_id: int | None
    agent_id: int | None
    from_state: str | None
    to_state: str | None
    payload: dict[str, Any]
