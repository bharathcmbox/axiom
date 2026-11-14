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
    python_version: str | None
    test_paths: list[str]
    created_at: datetime


class RequireCiIn(BaseModel):
    require_ci: bool


class TestPathsIn(BaseModel):
    test_paths: list[str] = Field(examples=[["agent/tests/**"]])


class ScheduleIn(BaseModel):
    specification: str = Field(min_length=1, max_length=20000)
    budget_usd: Decimal = Field(gt=0, description="The most each plan's tasks may total")
    every_hours: int = Field(ge=1, le=24 * 90, description="How often a plan is requested")
    first_at: datetime | None = Field(
        default=None, description="When it first fires (default: one interval from now)"
    )


class TimerOut(_Out):
    id: int
    kind: str
    status: str
    project_key: str
    due_at: datetime
    every_seconds: int | None
    specification: str | None
    budget_usd: Decimal | None
    task_key: str | None = None
    plan_id: int | None
    reason: str | None
    subject_state: str | None
    created_by: str
    created_at: datetime
    fired_count: int
    last_fired_at: datetime | None
    ended_by: str | None
    ended_at: datetime | None


class NotificationOut(_Out):
    id: int
    project_key: str
    reason: str
    task_key: str | None = None
    plan_id: int | None
    timer_id: int | None
    push: bool
    text: str
    created_at: datetime
    delivered_at: datetime | None
    attempts: int
    delivery_error: str | None


class PlanIn(BaseModel):
    specification: str = Field(min_length=1, max_length=20000)
    budget_usd: Decimal = Field(gt=0, description="The most the plan's tasks may total")


class PlanOut(_Out):
    id: int
    project_key: str
    specification: str
    budget_usd: Decimal
    status: str
    planner_key: str | None = None
    attempt_count: int
    feedback: list[str]
    contract: dict[str, Any] | None
    error: str | None
    cost_usd: Decimal | None
    cost_estimated: bool
    created_by: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    task_keys: dict[str, str] = {}


class PlanClaimIn(BaseModel):
    lease_seconds: int | None = Field(default=None, gt=0)


class PlanAssignmentOut(_Out):
    plan_id: int
    project_key: str
    repo_url: str | None
    default_branch: str
    specification: str
    budget_usd: Decimal
    test_paths: list[str]
    feedback: list[str]
    previous_plan: dict[str, Any] | None
    lease_expires_at: datetime
    run_budget_usd: Decimal
    run_number: int


class PlanSubmitIn(BaseModel):
    contract: dict[str, Any] = Field(description="Plan contract: summary, tasks, risks")
    cost: dict[str, Any] | None = None


class PlanFailIn(BaseModel):
    error: str = Field(min_length=1)
    cost: dict[str, Any] | None = None
    final: bool = Field(default=False, description="Out of budget: don't retry")


class PlanRejectIn(BaseModel):
    feedback: str = Field(min_length=1, description="What should change: the next attempt reads it")


class PlanCancelIn(BaseModel):
    reason: str | None = None


class PythonVersionIn(BaseModel):
    python_version: str | None = Field(examples=["3.12"])


class LateCostIn(BaseModel):
    cost: dict[str, Any] = Field(examples=[{"usd": "0.14", "estimated": True}])


class LateCostOut(BaseModel):
    recorded: bool


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
    cost_estimated: bool
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
    final: bool = Field(default=False, description="Out of budget: don't retry")


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
    cost_estimated: bool
    created_at: datetime
    completed_at: datetime | None


class DecisionOut(BaseModel):
    review_id: int
    decision: str = Field(
        description="Aggregator decision: wait, approve, request_changes, escalate."
    )


# ---- metrics (Phase 4) ---------------------------------------------------------------------


class TagsIn(BaseModel):
    tags: list[str]


class DefectIn(BaseModel):
    description: str = Field(min_length=1)
    severity: str = "medium"


class BaselineIn(BaseModel):
    project_key: str
    title: str = Field(min_length=1)
    minutes: float = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    review_rounds: int = Field(default=0, ge=0)
    notes: str = ""
