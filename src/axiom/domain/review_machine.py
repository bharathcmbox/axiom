"""Review state machine (ADR-009). Pure data and functions, no I/O.

A review is one reviewer role's verdict on one round of a task's work.
"""

from dataclasses import dataclass
from enum import StrEnum

from axiom.domain.roles import ActorKind


class ReviewState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SUPERSEDED = "SUPERSEDED"


TERMINAL = frozenset({ReviewState.PASS, ReviewState.FAIL, ReviewState.ERROR,
                      ReviewState.SUPERSEDED})  # fmt: skip
OPEN = frozenset({ReviewState.PENDING, ReviewState.CLAIMED})
MAX_REVIEW_ATTEMPTS = 3  # a review whose run fails or times out this often becomes ERROR


class ReviewCommand(StrEnum):
    CLAIM = "claim"
    SUBMIT = "submit"
    FAIL_RUN = "fail_run"  # the reviewer's run crashed or produced no valid contract
    TIME_OUT = "time_out"
    SUPERSEDE = "supersede"


@dataclass(frozen=True)
class ReviewRule:
    command: ReviewCommand
    sources: frozenset[ReviewState]
    targets: tuple[ReviewState, ...]
    actors: frozenset[ActorKind]
    event: str


_A = frozenset({ActorKind.AGENT})
_S = frozenset({ActorKind.SYSTEM})
R = ReviewState

REVIEW_RULES: dict[ReviewCommand, ReviewRule] = {
    r.command: r
    for r in (
        ReviewRule(ReviewCommand.CLAIM, frozenset({R.PENDING}), (R.CLAIMED,), _A,
                   "REVIEW_CLAIMED"),
        # target depends on the verdict: PASS or FAIL
        ReviewRule(ReviewCommand.SUBMIT, frozenset({R.CLAIMED}), (R.PASS, R.FAIL), _A,
                   "REVIEW_SUBMITTED"),
        # target depends on attempts left: back to PENDING, or ERROR
        ReviewRule(ReviewCommand.FAIL_RUN, frozenset({R.CLAIMED}), (R.PENDING, R.ERROR), _A,
                   "REVIEW_RUN_FAILED"),
        ReviewRule(ReviewCommand.TIME_OUT, frozenset({R.CLAIMED}), (R.PENDING, R.ERROR), _S,
                   "REVIEW_TIMED_OUT"),
        ReviewRule(ReviewCommand.SUPERSEDE, OPEN, (R.SUPERSEDED,), _S, "REVIEW_SUPERSEDED"),
    )
}  # fmt: skip


class IllegalReviewTransition(Exception):
    def __init__(self, command: ReviewCommand, state: ReviewState) -> None:
        self.command, self.state = command, state
        super().__init__(f"cannot {command} a review in {state}")


class ReviewActorNotPermitted(Exception):
    def __init__(self, command: ReviewCommand, kind: ActorKind) -> None:
        self.command, self.kind = command, kind
        super().__init__(f"{kind} may not {command} a review")


def check_review(command: ReviewCommand, state: ReviewState, kind: ActorKind) -> ReviewRule:
    rule = REVIEW_RULES[command]
    if state not in rule.sources:
        raise IllegalReviewTransition(command, state)
    if kind not in rule.actors:
        raise ReviewActorNotPermitted(command, kind)
    return rule


def next_review_state(
    command: ReviewCommand,
    state: ReviewState,
    kind: ActorKind,
    *,
    verdict: str | None = None,
    attempts_after: int = 0,
) -> ReviewState:
    rule = check_review(command, state, kind)
    if command == ReviewCommand.SUBMIT:
        if verdict not in ("PASS", "FAIL"):
            raise ValueError("submit needs verdict PASS or FAIL")
        return ReviewState(verdict)
    if command in (ReviewCommand.FAIL_RUN, ReviewCommand.TIME_OUT):
        retry, exhausted = rule.targets
        return retry if attempts_after < MAX_REVIEW_ATTEMPTS else exhausted
    return rule.targets[0]
