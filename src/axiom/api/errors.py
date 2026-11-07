"""The one place where domain errors become HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from axiom import errors as e
from axiom.domain.plan_machine import IllegalPlanTransition, PlanActorNotPermitted
from axiom.domain.review_machine import IllegalReviewTransition, ReviewActorNotPermitted
from axiom.domain.task_machine import ActorNotPermitted, IllegalTransition

STATUS: dict[type[Exception], int] = {
    e.NotFound: 404,
    e.AlreadyExists: 409,
    e.InvalidInput: 422,
    e.Forbidden: 403,
    ActorNotPermitted: 403,
    e.NotExecutionOwner: 403,
    IllegalTransition: 409,
    e.DependenciesNotMet: 409,
    e.AssignmentRejected: 409,
    e.LeaseExpired: 409,
    e.LeaseNotExpired: 409,
    e.NoActiveExecution: 409,
    IllegalReviewTransition: 409,
    ReviewActorNotPermitted: 403,
    IllegalPlanTransition: 409,
    PlanActorNotPermitted: 403,
}


def _code(exc: Exception) -> str:
    """Class name as a snake_case error code, e.g. IllegalTransition -> illegal_transition."""
    name = type(exc).__name__
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


async def _handle(request: Request, exc: Exception) -> JSONResponse:
    body: dict[str, object] = {"error": _code(exc), "detail": str(exc)}
    if isinstance(exc, IllegalTransition):
        body |= {"state": exc.state, "command": exc.command, "allowed": exc.allowed}
    return JSONResponse(status_code=STATUS[type(exc)], content=body)


def install(app: FastAPI) -> None:
    for exc_type in STATUS:
        app.add_exception_handler(exc_type, _handle)
