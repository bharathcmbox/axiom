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
    default_branch: str = "main"
    test_command: str | None = Field(
        default=None, examples=["cd agent && uv run --with pytest pytest -q"]
    )
    require_ci: bool = Field(
        default=False, description="Gate AWAITING_MERGE on a green CI run for the head commit."
    )


class ProjectOut(_Out):
    id: int
    key: str
    name: str
    repo_url: str | None
    specification: str | None
    settings: dict[str, Any]
    default_branch: str
    test_command: str | None
    require_ci: bool
    created_at: datetime


class AgentIn(BaseModel):
    key: str = Field(examples=["DEV-03"])
    role: Role
    runtime: Runtime
    model: str | None = None
    instructions_path: str | None = None
    max_concurrent: int = Field(default=1, ge=1)


class AgentModelIn(BaseModel):
    model: str | None = Field(examples=["claude-fable-5-1"])


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
    budget_usd: Decimal | None = Field(
        default=None, ge=0, description="Defaults to AXIOM_DEFAULT_TASK_BUDGET_USD."
    )
    dependencies: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(
        default_factory=list,
        description="Repository-relative paths or globs the developer may change.",
        examples=[["agent/pyproject.toml", "agent/uv.lock", "CLAUDE.md"]],
    )


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
    allowed_paths: list[str]
    head_sha: str | None
    ci_status: str | None
    ci_sha: str | None
    budget_spent_usd: Decimal
    run_budget_usd: Decimal = Field(
        description="What a new execution may spend now: min(task remaining, day remaining)."
    )
    created_at: datetime
    updated_at: datetime


class BudgetIn(BaseModel):
    budget_usd: Decimal = Field(ge=0)


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


# ---- reviews (Phase 3) ---------------------------------------------------------------------


class ReviewClaimIn(BaseModel):
    lease_seconds: int | None = Field(default=None, ge=1)


class ReviewAssignmentOut(BaseModel):
    review_id: int
    role: str
    round: int
    head_sha: str | None
    lease_expires_at: datetime
    run_budget_usd: Decimal
    task: dict[str, Any]
    developer_result: dict[str, Any] | None


class ReviewSubmitIn(BaseModel):
    contract: dict[str, Any] = Field(description="Review contract: verdict, summary, findings.")
    cost: dict[str, Any] | None = None


class ReviewFailIn(BaseModel):
    error: str = Field(min_length=1)
    cost: dict[str, Any] | None = None


class ReviewOut(BaseModel):
    id: int
    round: int
    role: str
    status: str
    head_sha: str | None
    reviewer_key: str | None
    attempt_count: int
    verdict: str | None
    contract: dict[str, Any] | None
    error: str | None
    cost_usd: Decimal | None
    created_at: datetime
    completed_at: datetime | None


class DecisionOut(BaseModel):
    review_id: int
    decision: str = Field(
        description="Aggregator decision: wait, approve, request_changes, escalate."
    )
