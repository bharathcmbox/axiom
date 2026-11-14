"""Timer state machine (ADR-018). Pure data and functions, no I/O.

A timer waits until it's due, then fires once. A recurring one (a schedule) stays PENDING with its
next due time; a one-off (a follow-up) becomes FIRED. Either can be cancelled while pending: by a
human, or by Axiom when what a follow-up was watching has moved on.
"""

from dataclasses import dataclass
from enum import StrEnum

from axiom.domain.roles import ActorKind


class TimerState(StrEnum):
    PENDING = "PENDING"
    FIRED = "FIRED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({TimerState.FIRED, TimerState.CANCELLED})


class TimerCommand(StrEnum):
    FIRE = "fire"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TimerRule:
    command: TimerCommand
    sources: frozenset[TimerState]
    targets: tuple[TimerState, ...]
    actors: frozenset[ActorKind]
    event: str


T = TimerState
TIMER_RULES: dict[TimerCommand, TimerRule] = {
    r.command: r
    for r in (
        # target depends on the timer: recurring stays PENDING (next occurrence), one-off is FIRED
        TimerRule(TimerCommand.FIRE, frozenset({T.PENDING}), (T.PENDING, T.FIRED),
                  frozenset({ActorKind.SYSTEM}), "TIMER_FIRED"),
        TimerRule(TimerCommand.CANCEL, frozenset({T.PENDING}), (T.CANCELLED,),
                  frozenset({ActorKind.HUMAN, ActorKind.SYSTEM}), "TIMER_CANCELLED"),
    )
}  # fmt: skip


class IllegalTimerTransition(Exception):
    def __init__(self, command: TimerCommand, state: TimerState) -> None:
        self.command, self.state = command, state
        super().__init__(f"cannot {command} a timer that is {state}")


class TimerActorNotPermitted(Exception):
    def __init__(self, command: TimerCommand, kind: ActorKind) -> None:
        self.command, self.kind = command, kind
        super().__init__(f"{kind} may not {command} a timer")


def check_timer(command: TimerCommand, state: TimerState, kind: ActorKind) -> TimerRule:
    rule = TIMER_RULES[command]
    if state not in rule.sources:
        raise IllegalTimerTransition(command, state)
    if kind not in rule.actors:
        raise TimerActorNotPermitted(command, kind)
    return rule


def next_timer_state(
    command: TimerCommand, state: TimerState, kind: ActorKind, *, recurring: bool = False
) -> TimerState:
    rule = check_timer(command, state, kind)
    if command == TimerCommand.FIRE:
        again, once = rule.targets
        return again if recurring else once
    return rule.targets[0]
