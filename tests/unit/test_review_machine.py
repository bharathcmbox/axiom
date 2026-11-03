"""Phase 3 T2: the review state machine, checked against docs/state-machines.md."""

import itertools

import pytest

from axiom.domain.review_machine import (
    MAX_REVIEW_ATTEMPTS,
    OPEN,
    REVIEW_RULES,
    TERMINAL,
    IllegalReviewTransition,
    ReviewActorNotPermitted,
    ReviewCommand,
    ReviewState,
    check_review,
    next_review_state,
)
from axiom.domain.roles import ActorKind
from tests.unit.doc_tables import review_rows

DOC = {row[0]: row for row in review_rows()}


def test_doc_and_code_agree() -> None:
    assert set(DOC) == {str(c) for c in ReviewCommand}
    for cmd, rule in REVIEW_RULES.items():
        _, sources, targets, actor, event = DOC[cmd]
        assert {str(s) for s in rule.sources} == sources, cmd
        assert tuple(str(t) for t in rule.targets) == targets, cmd
        assert {str(a) for a in rule.actors} == {actor}, cmd
        assert rule.event == event, cmd


@pytest.mark.parametrize(
    ("command", "state"), list(itertools.product(ReviewCommand, ReviewState)), ids=str
)
def test_every_command_state_pair(command: ReviewCommand, state: ReviewState) -> None:
    rule = REVIEW_RULES[command]
    kind = next(iter(rule.actors))
    if state in rule.sources:
        assert check_review(command, state, kind) is rule
    else:
        with pytest.raises(IllegalReviewTransition):
            check_review(command, state, kind)


@pytest.mark.parametrize(
    ("command", "kind"), list(itertools.product(ReviewCommand, ActorKind)), ids=str
)
def test_actor_kinds(command: ReviewCommand, kind: ActorKind) -> None:
    rule = REVIEW_RULES[command]
    state = next(iter(rule.sources))
    if kind in rule.actors:
        check_review(command, state, kind)
    else:
        with pytest.raises(ReviewActorNotPermitted):
            check_review(command, state, kind)


def test_submit_target_follows_the_verdict() -> None:
    a = ActorKind.AGENT
    assert next_review_state(ReviewCommand.SUBMIT, ReviewState.CLAIMED, a, verdict="PASS") == "PASS"
    assert next_review_state(ReviewCommand.SUBMIT, ReviewState.CLAIMED, a, verdict="FAIL") == "FAIL"
    with pytest.raises(ValueError):
        next_review_state(ReviewCommand.SUBMIT, ReviewState.CLAIMED, a, verdict="MAYBE")


@pytest.mark.parametrize("command", [ReviewCommand.FAIL_RUN, ReviewCommand.TIME_OUT])
def test_retry_then_error(command: ReviewCommand) -> None:
    kind = next(iter(REVIEW_RULES[command].actors))
    for attempts in range(1, MAX_REVIEW_ATTEMPTS):
        assert next_review_state(command, ReviewState.CLAIMED, kind, attempts_after=attempts) == (
            ReviewState.PENDING
        )
    assert (
        next_review_state(command, ReviewState.CLAIMED, kind, attempts_after=MAX_REVIEW_ATTEMPTS)
        == ReviewState.ERROR
    )


def test_terminal_and_open_states() -> None:
    assert set(ReviewState) == TERMINAL | OPEN and not TERMINAL & OPEN
    for state in TERMINAL:
        for command in ReviewCommand:
            with pytest.raises(IllegalReviewTransition):
                check_review(command, state, next(iter(REVIEW_RULES[command].actors)))
