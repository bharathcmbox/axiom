"""REST routes. Thin: parse, call a service, serialize. No business rules here."""

from collections.abc import Iterator
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from axiom.api.schemas import (
    AgentIn,
    AgentModelIn,
    AgentOut,
    BaselineIn,
    BudgetIn,
    ClaimIn,
    CommandIn,
    DecisionOut,
    DefectIn,
    EventOut,
    ExecutionOut,
    HeartbeatIn,
    HeartbeatOut,
    LateCostIn,
    LateCostOut,
    NotificationOut,
    PlanAssignmentOut,
    PlanCancelIn,
    PlanClaimIn,
    PlanFailIn,
    PlanIn,
    PlanOut,
    PlanRejectIn,
    PlanSubmitIn,
    ProjectIn,
    ProjectOut,
    PythonVersionIn,
    RequireCiIn,
    ReviewAssignmentOut,
    ReviewClaimIn,
    ReviewFailIn,
    ReviewOut,
    ReviewSubmitIn,
    ScheduleIn,
    TagsIn,
    TaskIn,
    TaskOut,
    TestPathsIn,
    TimerOut,
)
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import Forbidden
from axiom.events.log import list_events
from axiom.orchestrator import measures, notifications, plans, reviews, timers
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.commands import apply_command, record_execution_cost
from axiom.orchestrator.leases import heartbeat
from axiom.orchestrator.tasks import Task, create_task, get_task, list_tasks, set_task_budget
from axiom.registry import agents, projects
from axiom.registry.tokens import authenticate
from axiom.state.db import Conn
from axiom.state.invariants import check_invariants

# /healthz is the only unauthenticated route; everything on `router` needs a valid token.
public_router = APIRouter()


def get_conn(request: Request) -> Iterator[Conn]:
    with request.app.state.pool.connection() as conn:
        yield conn


def get_actor(
    request: Request,
    authorization: Annotated[
        str | None,
        Header(description="Bearer token issued with `python -m axiom.registry.tokens issue`"),
    ] = None,
) -> Actor:
    """Authenticate the request. The actor comes from the token, never from the client."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with request.app.state.pool.connection() as conn:
        actor = authenticate(conn, token.strip())
    if actor is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return actor


ConnDep = Annotated[Conn, Depends(get_conn)]
ActorDep = Annotated[Actor, Depends(get_actor)]
router = APIRouter(dependencies=[Depends(get_actor)])

# Commands exposed on /tasks/{key}/commands/{command}. `claim` is POST /claims. `promote` and
# `time_out` are system commands, issued only inside the control plane (system actors cannot
# authenticate over HTTP).
INTERNAL_COMMANDS = (TaskCommand.CLAIM, TaskCommand.PROMOTE, TaskCommand.TIME_OUT)
PUBLIC_COMMANDS = [c for c in TaskCommand if c not in INTERNAL_COMMANDS]


def _task_out(task: Task) -> TaskOut:
    return TaskOut.model_validate(asdict(task) | {"dependencies": list(task.dependencies)})


# ---- health ---------------------------------------------------------------------------------


@public_router.get("/healthz", tags=["ops"])
def healthz(conn: ConnDep) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok", "database": "ok"}


@router.get("/invariants", tags=["ops"])
def invariants(conn: ConnDep) -> dict[str, list[str]]:
    return {"violations": check_invariants(conn)}


# ---- projects -------------------------------------------------------------------------------


@router.post("/projects", status_code=201, tags=["projects"])
def create_project(body: ProjectIn, conn: ConnDep, actor: ActorDep) -> ProjectOut:
    p = projects.create_project(conn, actor, **body.model_dump())
    return ProjectOut.model_validate(p)


@router.get("/projects", tags=["projects"])
def list_projects(conn: ConnDep) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in projects.list_projects(conn)]


@router.get("/projects/{key}", tags=["projects"])
def get_project(key: str, conn: ConnDep) -> ProjectOut:
    return ProjectOut.model_validate(projects.get_project(conn, key))


@router.post("/projects/{key}/python-version", tags=["projects"])
def set_python_version_route(
    key: str, body: PythonVersionIn, conn: ConnDep, actor: ActorDep
) -> ProjectOut:
    """Which Python uv uses in this project's sandboxes, to match its CI (humans only)."""
    return ProjectOut.model_validate(
        projects.set_python_version(conn, actor, key, body.python_version)
    )


@router.post("/projects/{key}/test-paths", tags=["projects"])
def set_test_paths_route(key: str, body: TestPathsIn, conn: ConnDep, actor: ActorDep) -> ProjectOut:
    """What a QA task may touch in this project (humans only)."""
    return ProjectOut.model_validate(projects.set_test_paths(conn, actor, key, body.test_paths))


@router.post("/projects/{key}/plans", tags=["plans"], status_code=201)
def request_plan_route(key: str, body: PlanIn, conn: ConnDep, actor: ActorDep) -> PlanOut:
    """Submit a spec for planning (humans only). Nothing runs until you approve the plan."""
    plan = plans.request_plan(
        conn, actor, project_key=key, specification=body.specification, budget_usd=body.budget_usd
    )
    return PlanOut.model_validate(plans.get_plan(conn, plan["id"]))


@router.post("/projects/{key}/schedules", tags=["timers"], status_code=201)
def create_schedule_route(key: str, body: ScheduleIn, conn: ConnDep, actor: ActorDep) -> TimerOut:
    """A plan requested every N hours (humans only, ADR-018). Each plan still waits for your
    approval before anything runs."""
    timer = timers.create_schedule(
        conn, actor, project_key=key, specification=body.specification,
        budget_usd=body.budget_usd, every_hours=body.every_hours, first_at=body.first_at,
    )  # fmt: skip
    return _timer(conn, timer["id"])


@router.get("/timers", tags=["timers"])
def list_timers_route(
    conn: ConnDep, status: Annotated[str | None, Query()] = None
) -> list[TimerOut]:
    """Schedules and follow-ups."""
    return [TimerOut.model_validate(t) for t in timers.list_timers(conn, status)]


@router.post("/timers/{timer_id}/cancel", tags=["timers"])
def cancel_timer_route(timer_id: int, conn: ConnDep, actor: ActorDep) -> TimerOut:
    """Stop a schedule, or a reminder (humans only)."""
    if actor.kind != ActorKind.HUMAN:
        raise Forbidden("only humans cancel timers")
    timers.cancel_timer(conn, actor, timer_id)
    return _timer(conn, timer_id)


def _timer(conn: Conn, timer_id: int) -> TimerOut:
    return TimerOut.model_validate(next(t for t in timers.list_timers(conn) if t["id"] == timer_id))


@router.get("/notifications", tags=["timers"])
def list_notifications_route(
    conn: ConnDep, undelivered: Annotated[bool, Query()] = False
) -> list[NotificationOut]:
    """What Axiom has told you, or will (ADR-019), newest first, with the fixed text it posts."""
    return [
        NotificationOut.model_validate(n | {"text": notifications.comment_text(n)})
        for n in notifications.list_notifications(conn, undelivered=undelivered)
    ]


@router.get("/plans", tags=["plans"])
def list_plans_route(
    conn: ConnDep,
    project: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> list[PlanOut]:
    return [
        PlanOut.model_validate(p)
        for p in plans.list_plans(conn, project_key=project, status=status)
    ]


@router.post("/plans/claim", tags=["plans"], responses={204: {"description": "No plan"}})
def claim_plan_route(
    conn: ConnDep, actor: ActorDep, response: Response, body: PlanClaimIn | None = None
) -> PlanAssignmentOut | None:
    """A planner agent claims the oldest requested plan."""
    assignment = plans.claim_plan(conn, actor, lease_seconds=(body or PlanClaimIn()).lease_seconds)
    if assignment is None:
        response.status_code = 204
        return None
    return PlanAssignmentOut.model_validate(assignment, from_attributes=True)


@router.get("/plans/{plan_id}", tags=["plans"])
def get_plan_route(plan_id: int, conn: ConnDep) -> PlanOut:
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.get("/plans/{plan_id}/events", tags=["plans"])
def plan_events_route(plan_id: int, conn: ConnDep) -> list[EventOut]:
    return [EventOut.model_validate(e) for e in plans.plan_events(conn, plan_id)]


@router.post("/plans/{plan_id}/heartbeat", tags=["plans"])
def plan_heartbeat_route(
    plan_id: int, conn: ConnDep, actor: ActorDep, body: PlanClaimIn | None = None
) -> dict[str, Any]:
    expires = plans.plan_heartbeat(
        conn, actor, plan_id, lease_seconds=(body or PlanClaimIn()).lease_seconds
    )
    return {"plan_id": plan_id, "lease_expires_at": expires}


@router.post("/plans/{plan_id}/submit", tags=["plans"])
def submit_plan_route(plan_id: int, body: PlanSubmitIn, conn: ConnDep, actor: ActorDep) -> PlanOut:
    """A planner submits its proposal. If it fails the checks, it counts as a failed attempt."""
    payload = {"cost": body.cost} if body.cost else None
    plans.submit_plan(conn, actor, plan_id, body.contract, payload)
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/plans/{plan_id}/fail", tags=["plans"])
def fail_plan_route(plan_id: int, body: PlanFailIn, conn: ConnDep, actor: ActorDep) -> PlanOut:
    payload = {"cost": body.cost} if body.cost else None
    plans.fail_plan_run(conn, actor, plan_id, body.error, payload, final=body.final)
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/plans/{plan_id}/cost", tags=["plans"])
def record_plan_cost_route(
    plan_id: int, body: LateCostIn, conn: ConnDep, actor: ActorDep
) -> PlanOut:
    """The cost of a planning run that ended without reporting."""
    plans.record_plan_cost(conn, actor, plan_id, {"cost": body.cost})
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/plans/{plan_id}/approve", tags=["plans"])
def approve_plan_route(plan_id: int, conn: ConnDep, actor: ActorDep) -> PlanOut:
    """Create the plan's tasks, all or none (humans only)."""
    plans.approve_plan(conn, actor, plan_id)
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/plans/{plan_id}/reject", tags=["plans"])
def reject_plan_route(plan_id: int, body: PlanRejectIn, conn: ConnDep, actor: ActorDep) -> PlanOut:
    """Send it back with what should change (humans only)."""
    plans.reject_plan(conn, actor, plan_id, body.feedback)
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/plans/{plan_id}/cancel", tags=["plans"])
def cancel_plan_route(
    plan_id: int, conn: ConnDep, actor: ActorDep, body: PlanCancelIn | None = None
) -> PlanOut:
    plans.cancel_plan(conn, actor, plan_id, (body or PlanCancelIn()).reason)
    return PlanOut.model_validate(plans.get_plan(conn, plan_id))


@router.post("/projects/{key}/require-ci", tags=["projects"])
def set_require_ci_route(key: str, body: RequireCiIn, conn: ConnDep, actor: ActorDep) -> ProjectOut:
    """Turn the CI gate on or off for a project (humans only)."""
    return ProjectOut.model_validate(projects.set_require_ci(conn, actor, key, body.require_ci))


# ---- agents ---------------------------------------------------------------------------------


@router.post("/agents", status_code=201, tags=["agents"])
def register_agent(body: AgentIn, conn: ConnDep, actor: ActorDep) -> AgentOut:
    a = agents.register_agent(conn, actor, **body.model_dump(mode="json"))
    return AgentOut.model_validate(a)


@router.get("/agents", tags=["agents"])
def list_agents(
    conn: ConnDep, role: str | None = None, enabled: bool | None = None
) -> list[AgentOut]:
    return [
        AgentOut.model_validate(a) for a in agents.list_agents(conn, role=role, enabled=enabled)
    ]


@router.get("/agents/{key}", tags=["agents"])
def get_agent(key: str, conn: ConnDep) -> AgentOut:
    return AgentOut.model_validate(agents.get_agent(conn, key))


@router.post("/agents/{key}/enable", tags=["agents"])
def enable_agent(key: str, conn: ConnDep, actor: ActorDep) -> AgentOut:
    return AgentOut.model_validate(agents.set_enabled(conn, actor, key, True))


@router.post("/agents/{key}/model", tags=["agents"])
def set_agent_model(key: str, body: AgentModelIn, conn: ConnDep, actor: ActorDep) -> AgentOut:
    """Change an agent's model (humans only)."""
    return AgentOut.model_validate(agents.set_model(conn, actor, key, body.model))


@router.post("/agents/{key}/disable", tags=["agents"])
def disable_agent(key: str, conn: ConnDep, actor: ActorDep) -> AgentOut:
    return AgentOut.model_validate(agents.set_enabled(conn, actor, key, False))


# ---- tasks ----------------------------------------------------------------------------------


@router.post("/projects/{project_key}/tasks", status_code=201, tags=["tasks"])
def create_task_route(project_key: str, body: TaskIn, conn: ConnDep, actor: ActorDep) -> TaskOut:
    task = create_task(conn, actor, project_key=project_key, **body.model_dump(mode="python"))
    return _task_out(task)


@router.get("/tasks", tags=["tasks"])
def list_tasks_route(
    conn: ConnDep, project: str | None = None, state: TaskState | None = None
) -> list[TaskOut]:
    return [_task_out(t) for t in list_tasks(conn, project_key=project, state=state)]


@router.get("/tasks/{key}", tags=["tasks"])
def get_task_route(key: str, conn: ConnDep) -> TaskOut:
    return _task_out(get_task(conn, key))


@router.post("/tasks/{key}/commands/{command}", tags=["tasks"])
def command_route(
    key: str,
    command: str,
    conn: ConnDep,
    actor: ActorDep,
    body: CommandIn | None = None,
) -> TaskOut:
    """Apply a state-machine command (see docs/state-machines.md). Illegal commands return 409
    with the commands allowed from the task's current state."""
    try:
        cmd = TaskCommand(command)
    except ValueError:
        cmd = None
    if cmd not in PUBLIC_COMMANDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown command {command!r}; one of {[str(c) for c in PUBLIC_COMMANDS]}",
        )
    assert cmd is not None
    body = body or CommandIn()
    task = apply_command(
        conn,
        actor,
        key,
        cmd,
        agent_key=body.agent_key,
        payload=body.payload,
        lease_seconds=body.lease_seconds,
    )
    return _task_out(task)


@router.post("/tasks/{key}/budget", tags=["tasks"])
def budget_route(key: str, body: BudgetIn, conn: ConnDep, actor: ActorDep) -> TaskOut:
    """Change a task's budget (humans only), e.g. before retrying a task that ran out."""
    return _task_out(set_task_budget(conn, actor, key, body.budget_usd))


@router.post("/claims", tags=["tasks"], responses={204: {"description": "No READY task"}})
def claim_route(
    conn: ConnDep, actor: ActorDep, response: Response, body: ClaimIn | None = None
) -> TaskOut | None:
    """An agent claims the next READY task for its role (highest priority, oldest first)."""
    body = body or ClaimIn()
    task = claim_next(conn, actor, project_key=body.project_key, lease_seconds=body.lease_seconds)
    if task is None:
        response.status_code = 204
        return None
    return _task_out(task)


@router.post("/tasks/{key}/heartbeat", tags=["tasks"])
def heartbeat_route(
    key: str, conn: ConnDep, actor: ActorDep, body: HeartbeatIn | None = None
) -> HeartbeatOut:
    body = body or HeartbeatIn()
    expires = heartbeat(conn, actor, key, lease_seconds=body.lease_seconds)
    return HeartbeatOut(task_key=key, lease_expires_at=expires)


@router.get("/tasks/{key}/executions", tags=["tasks"])
def task_executions(key: str, conn: ConnDep) -> list[ExecutionOut]:
    task = get_task(conn, key)
    rows: list[dict[str, Any]] = conn.execute(
        "SELECT * FROM executions WHERE task_id = %s ORDER BY id", (task.id,)
    ).fetchall()
    return [ExecutionOut.model_validate(r) for r in rows]


# ---- events ---------------------------------------------------------------------------------


@router.get("/tasks/{key}/events", tags=["events"])
def task_events(key: str, conn: ConnDep) -> list[EventOut]:
    task = get_task(conn, key)
    return [EventOut.model_validate(e) for e in list_events(conn, task_id=task.id)]


@router.get("/events", tags=["events"])
def events(
    conn: ConnDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[EventOut]:
    return [EventOut.model_validate(e) for e in list_events(conn, after_id=after_id, limit=limit)]


# ---- reviews (Phase 3) ---------------------------------------------------------------------


@router.post("/reviews/claim", tags=["reviews"], responses={204: {"description": "No review"}})
def claim_review_route(
    conn: ConnDep, actor: ActorDep, response: Response, body: ReviewClaimIn | None = None
) -> ReviewAssignmentOut | None:
    """A reviewer agent claims the oldest pending review for its role."""
    body = body or ReviewClaimIn()
    assignment = reviews.claim_review(conn, actor, lease_seconds=body.lease_seconds)
    if assignment is None:
        response.status_code = 204
        return None
    return ReviewAssignmentOut.model_validate(assignment, from_attributes=True)


@router.post("/reviews/{review_id}/heartbeat", tags=["reviews"])
def review_heartbeat_route(
    review_id: int, conn: ConnDep, actor: ActorDep, body: ReviewClaimIn | None = None
) -> dict[str, Any]:
    expires = reviews.review_heartbeat(
        conn, actor, review_id, lease_seconds=(body or ReviewClaimIn()).lease_seconds
    )
    return {"review_id": review_id, "lease_expires_at": expires}


@router.post("/reviews/{review_id}/submit", tags=["reviews"])
def submit_review_route(
    review_id: int, body: ReviewSubmitIn, conn: ConnDep, actor: ActorDep
) -> DecisionOut:
    """Submit a verdict. The response says what the aggregator did with the task."""
    payload = {"cost": body.cost} if body.cost else None
    decision = reviews.submit_review(conn, actor, review_id, body.contract, payload)
    return DecisionOut(review_id=review_id, decision=decision)


@router.post("/reviews/{review_id}/fail", tags=["reviews"])
def fail_review_route(
    review_id: int, body: ReviewFailIn, conn: ConnDep, actor: ActorDep
) -> DecisionOut:
    payload = {"cost": body.cost} if body.cost else None
    decision = reviews.fail_review_run(
        conn, actor, review_id, body.error, payload, final=body.final
    )
    return DecisionOut(review_id=review_id, decision=decision)


@router.post("/reviews/{review_id}/cost", tags=["reviews"])
def record_review_cost_route(
    review_id: int, body: LateCostIn, conn: ConnDep, actor: ActorDep
) -> LateCostOut:
    """The cost of a run that ended without reporting. The first report wins."""
    return LateCostOut(
        recorded=reviews.record_review_cost(conn, actor, review_id, {"cost": body.cost})
    )


@router.post("/executions/{execution_id}/cost", tags=["tasks"])
def record_execution_cost_route(
    execution_id: int, body: LateCostIn, conn: ConnDep, actor: ActorDep
) -> LateCostOut:
    """The cost of a run that ended without reporting. The first report wins."""
    return LateCostOut(
        recorded=record_execution_cost(conn, actor, execution_id, {"cost": body.cost})
    )


@router.get("/tasks/{key}/reviews", tags=["reviews"])
def task_reviews_route(key: str, conn: ConnDep) -> list[ReviewOut]:
    task = get_task(conn, key)
    return [ReviewOut.model_validate(r) for r in reviews.list_reviews(conn, task.id)]


# ---- metrics (Phase 4, ADR-012) ------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


@router.get("/metrics", tags=["metrics"])
def metrics_route(
    conn: ConnDep, project: str | None = None, include_demo: bool = False
) -> dict[str, Any]:
    """Delivery metrics computed from the event log. Demo work is excluded unless asked for.
    `waiting_for_human_s` is elapsed time: an upper bound on attention, not a measure of it."""
    per_task, summary, baselines = measures.collect(
        conn, project_key=project, include_demo=include_demo
    )
    return {
        "summary": {k: _jsonable(v) for k, v in asdict(summary).items()},
        "tasks": [
            {k: _jsonable(v) for k, v in asdict(m).items()} | {"cost_usd": str(m.cost_usd)}
            for m in per_task
        ],
        "baselines": baselines,
        "plans": [
            {k: _jsonable(v) for k, v in asdict(d).items()}
            | {"saved_by_parallel_s": d.saved_by_parallel_s}
            for d in measures.collect_plans(conn, project_key=project, include_demo=include_demo)
        ],
    }


@router.post("/tasks/{key}/tags", tags=["metrics"])
def tags_route(key: str, body: TagsIn, conn: ConnDep, actor: ActorDep) -> dict[str, list[str]]:
    return {"tags": measures.set_tags(conn, actor, key, body.tags)}


@router.post("/tasks/{key}/defects", status_code=201, tags=["metrics"])
def defects_route(key: str, body: DefectIn, conn: ConnDep, actor: ActorDep) -> dict[str, str]:
    measures.record_defect(conn, actor, key, body.description, body.severity)
    return {"status": "recorded"}


@router.post("/baselines", status_code=201, tags=["metrics"])
def baselines_route(body: BaselineIn, conn: ConnDep, actor: ActorDep) -> dict[str, str]:
    measures.record_baseline(conn, actor, **body.model_dump())
    return {"status": "recorded"}
