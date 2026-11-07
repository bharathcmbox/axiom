"""Phase 4 T6: the plan state machine, checked against docs/state-machines.md."""

import itertools

import pytest

from axiom.domain.plan_machine import (
    MAX_PLAN_ATTEMPTS,
    PLAN_RULES,
    TERMINAL,
    IllegalPlanTransition,
    PlanActorNotPermitted,
    PlanCommand,
    PlanState,
    check_plan,
    next_plan_state,
)
from axiom.domain.roles import ActorKind
from tests.unit.doc_tables import plan_rows

DOC = {row[0]: row for row in plan_rows()}


def test_doc_and_code_agree() -> None:
    assert set(DOC) == {str(c) for c in PlanCommand}
    for cmd, rule in PLAN_RULES.items():
        _, sources, targets, actor, event = DOC[cmd]
        assert {str(s) for s in rule.sources} == sources, cmd
        assert tuple(str(t) for t in rule.targets) == targets, cmd
        assert {str(a) for a in rule.actors} == {actor}, cmd
        assert rule.event == event, cmd


@pytest.mark.parametrize(
    ("command", "state"), list(itertools.product(PlanCommand, PlanState)), ids=str
)
def test_every_command_state_pair(command: PlanCommand, state: PlanState) -> None:
    rule = PLAN_RULES[command]
    kind = next(iter(rule.actors))
    if state in rule.sources:
        assert check_plan(command, state, kind) is rule
    else:
        with pytest.raises(IllegalPlanTransition):
            check_plan(command, state, kind)


@pytest.mark.parametrize(
    ("command", "kind"), list(itertools.product(PlanCommand, ActorKind)), ids=str
)
def test_actor_kinds(command: PlanCommand, kind: ActorKind) -> None:
    rule = PLAN_RULES[command]
    state = next(iter(rule.sources))
    if kind in rule.actors:
        check_plan(command, state, kind)
    else:
        with pytest.raises(PlanActorNotPermitted):
            check_plan(command, state, kind)


def test_only_a_human_can_approve_reject_or_cancel() -> None:
    for command in (PlanCommand.APPROVE, PlanCommand.REJECT, PlanCommand.CANCEL):
        assert PLAN_RULES[command].actors == {ActorKind.HUMAN}


@pytest.mark.parametrize("command", [PlanCommand.FAIL_RUN, PlanCommand.TIME_OUT])
def test_retry_then_error(command: PlanCommand) -> None:
    kind = next(iter(PLAN_RULES[command].actors))
    for attempts in range(1, MAX_PLAN_ATTEMPTS):
        assert next_plan_state(command, PlanState.CLAIMED, kind, attempts_after=attempts) == (
            PlanState.REQUESTED
        )
    assert (
        next_plan_state(command, PlanState.CLAIMED, kind, attempts_after=MAX_PLAN_ATTEMPTS)
        == PlanState.ERROR
    )


def test_terminal_states_allow_nothing() -> None:
    for state in TERMINAL:
        for command in PlanCommand:
            with pytest.raises(IllegalPlanTransition):
                check_plan(command, state, next(iter(PLAN_RULES[command].actors)))
