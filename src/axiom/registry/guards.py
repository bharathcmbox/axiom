"""Shared authorization checks for the registry."""

from axiom.domain.roles import Actor, ActorKind
from axiom.errors import Forbidden


def require_not_agent(actor: Actor) -> None:
    """The registry is managed by humans (or in-process system tasks), never by agents."""
    if actor.kind == ActorKind.AGENT:
        raise Forbidden("agents cannot change the registry")
