"""Phase 5 T4: the timer state machine, checked against docs/state-machines.md."""

import itertools

import pytest

from axiom.domain.roles import ActorKind
from axiom.domain.timer_machine import (
    TIMER_RULES,
    IllegalTimerTransition,
    TimerActorNotPermitted,
    TimerCommand,
    TimerState,
    check_timer,
    next_timer_state,
)
from tests.unit.doc_tables import timer_rows

DOC = {row[0]: row for row in timer_rows()}


def test_doc_and_code_agree() -> None:
    assert set(DOC) == {str(c) for c in TimerCommand}
    for cmd, rule in TIMER_RULES.items():
        _, sources, targets, actors, event = DOC[cmd]
        assert {str(s) for s in rule.sources} == sources, cmd
        assert tuple(str(t) for t in rule.targets) == targets, cmd
        assert {str(a) for a in rule.actors} == actors, cmd
        assert rule.event == event, cmd


@pytest.mark.parametrize(
    ("command", "state"), list(itertools.product(TimerCommand, TimerState)), ids=str
)
def test_every_command_state_pair(command: TimerCommand, state: TimerState) -> None:
    rule = TIMER_RULES[command]
    kind = next(iter(rule.actors))
    if state in rule.sources:
        assert check_timer(command, state, kind) is rule
    else:
        with pytest.raises(IllegalTimerTransition):
            check_timer(command, state, kind)


@pytest.mark.parametrize(
    ("command", "kind"), list(itertools.product(TimerCommand, ActorKind)), ids=str
)
def test_actor_kinds(command: TimerCommand, kind: ActorKind) -> None:
    rule = TIMER_RULES[command]
    if kind in rule.actors:
        check_timer(command, TimerState.PENDING, kind)
    else:
        with pytest.raises(TimerActorNotPermitted):
            check_timer(command, TimerState.PENDING, kind)


def test_no_agent_can_touch_a_timer() -> None:
    assert all(ActorKind.AGENT not in r.actors for r in TIMER_RULES.values())


def test_a_schedule_stays_pending_and_a_follow_up_is_done() -> None:
    fire, s, sys = TimerCommand.FIRE, TimerState.PENDING, ActorKind.SYSTEM
    assert next_timer_state(fire, s, sys, recurring=True) == TimerState.PENDING
    assert next_timer_state(fire, s, sys, recurring=False) == TimerState.FIRED
