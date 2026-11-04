"""REST routes. Thin: parse, call a service, serialize. No business rules here."""

from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from axiom.api.schemas import (
    AgentIn,
    AgentModelIn,
    AgentOut,
    BudgetIn,
    ClaimIn,
    CommandIn,
    DecisionOut,
    EventOut,
    ExecutionOut,
    HeartbeatIn,
    HeartbeatOut,
    ProjectIn,
    ProjectOut,
    ReviewAssignmentOut,
    ReviewClaimIn,
    ReviewFailIn,
    ReviewOut,
    ReviewSubmitIn,
    TaskIn,
    TaskOut,
)
from axiom.domain.roles import Actor
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.events.log import list_events
from axiom.orchestrator import reviews
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.commands import apply_command
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
    decision = reviews.fail_review_run(conn, actor, review_id, body.error, payload)
    return DecisionOut(review_id=review_id, decision=decision)


@router.get("/tasks/{key}/reviews", tags=["reviews"])
def task_reviews_route(key: str, conn: ConnDep) -> list[ReviewOut]:
    task = get_task(conn, key)
    return [ReviewOut.model_validate(r) for r in reviews.list_reviews(conn, task.id)]
