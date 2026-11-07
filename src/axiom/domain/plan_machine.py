"""Plan state machine (ADR-011). Pure data and functions, no I/O.

A plan is a human's specification, a planner's proposal, and a human's decision. A rejection isn't a
failure: it goes back to REQUESTED with feedback, and the attempts start over.
"""

from dataclasses import dataclass
from enum import StrEnum

from axiom.domain.roles import ActorKind


class PlanState(StrEnum):
    REQUESTED = "REQUESTED"
    CLAIMED = "CLAIMED"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({PlanState.APPROVED, PlanState.ERROR, PlanState.CANCELLED})
MAX_PLAN_ATTEMPTS = 3  # a plan whose run fails, times out or is invalid this often becomes ERROR


class PlanCommand(StrEnum):
    CLAIM = "claim"
    SUBMIT = "submit"
    FAIL_RUN = "fail_run"  # the run crashed, or its plan failed validation
    TIME_OUT = "time_out"
    REJECT = "reject"
    APPROVE = "approve"
    CANCEL = "cancel"


@dataclass(frozen=True)
class PlanRule:
    command: PlanCommand
    sources: frozenset[PlanState]
    targets: tuple[PlanState, ...]
    actors: frozenset[ActorKind]
    event: str


_A = frozenset({ActorKind.AGENT})
_S = frozenset({ActorKind.SYSTEM})
_H = frozenset({ActorKind.HUMAN})
P = PlanState

PLAN_RULES: dict[PlanCommand, PlanRule] = {
    r.command: r
    for r in (
        PlanRule(PlanCommand.CLAIM, frozenset({P.REQUESTED}), (P.CLAIMED,), _A, "PLAN_CLAIMED"),
        PlanRule(PlanCommand.SUBMIT, frozenset({P.CLAIMED}), (P.PROPOSED,), _A, "PLAN_PROPOSED"),
        # target depends on attempts left: back to REQUESTED, or ERROR
        PlanRule(PlanCommand.FAIL_RUN, frozenset({P.CLAIMED}), (P.REQUESTED, P.ERROR), _A,
                 "PLAN_RUN_FAILED"),
        PlanRule(PlanCommand.TIME_OUT, frozenset({P.CLAIMED}), (P.REQUESTED, P.ERROR), _S,
                 "PLAN_TIMED_OUT"),
        PlanRule(PlanCommand.REJECT, frozenset({P.PROPOSED}), (P.REQUESTED,), _H, "PLAN_REJECTED"),
        PlanRule(PlanCommand.APPROVE, frozenset({P.PROPOSED}), (P.APPROVED,), _H, "PLAN_APPROVED"),
        PlanRule(PlanCommand.CANCEL, frozenset({P.REQUESTED, P.PROPOSED}), (P.CANCELLED,), _H,
                 "PLAN_CANCELLED"),
    )
}  # fmt: skip


class IllegalPlanTransition(Exception):
    def __init__(self, command: PlanCommand, state: PlanState) -> None:
        self.command, self.state = command, state
        super().__init__(f"cannot {command} a plan in {state}")


class PlanActorNotPermitted(Exception):
    def __init__(self, command: PlanCommand, kind: ActorKind) -> None:
        self.command, self.kind = command, kind
        super().__init__(f"{kind} may not {command} a plan")


def check_plan(command: PlanCommand, state: PlanState, kind: ActorKind) -> PlanRule:
    rule = PLAN_RULES[command]
    if state not in rule.sources:
        raise IllegalPlanTransition(command, state)
    if kind not in rule.actors:
        raise PlanActorNotPermitted(command, kind)
    return rule


def next_plan_state(
    command: PlanCommand, state: PlanState, kind: ActorKind, *, attempts_after: int = 0
) -> PlanState:
    rule = check_plan(command, state, kind)
    if command in (PlanCommand.FAIL_RUN, PlanCommand.TIME_OUT):
        retry, exhausted = rule.targets
        return retry if attempts_after < MAX_PLAN_ATTEMPTS else exhausted
    return rule.targets[0]
