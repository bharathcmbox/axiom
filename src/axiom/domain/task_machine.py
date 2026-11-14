"""Task state machine: the single source of truth for which task commands are legal.

Pure data and functions, no I/O. docs/state-machines.md documents the same table; a unit test
keeps the two in sync.
"""

from dataclasses import dataclass
from enum import StrEnum

from axiom.domain.roles import ActorKind


class TaskState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    IN_REVIEW = "IN_REVIEW"
    AWAITING_MERGE = "AWAITING_MERGE"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.CANCELLED})
NON_TERMINAL_STATES = frozenset(set(TaskState) - TERMINAL_STATES)
# A task is in one of these states iff it has exactly one active execution.
EXECUTING_STATES = frozenset({TaskState.ASSIGNED, TaskState.IN_PROGRESS})
INITIAL_STATE = TaskState.PENDING


class TaskCommand(StrEnum):
    PROMOTE = "promote"
    CLAIM = "claim"
    ASSIGN = "assign"
    START = "start"
    SUBMIT = "submit"
    ASK_HUMAN = "ask_human"
    ANSWER = "answer"
    FAIL = "fail"
    TIME_OUT = "time_out"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT_MERGE = "reject_merge"
    REPORT_CONFLICT = "report_conflict"
    REREVIEW = "rereview"
    MERGE = "merge"
    RETRY = "retry"
    CANCEL = "cancel"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Rule:
    command: TaskCommand
    sources: frozenset[TaskState]
    # One target, except for retry-dependent commands: (target while retries remain, target
    # once retries are used up).
    targets: tuple[TaskState, ...]
    actors: frozenset[ActorKind]
    event: str

    @property
    def retry_dependent(self) -> bool:
        return len(self.targets) == 2


_H = frozenset({ActorKind.HUMAN})
_A = frozenset({ActorKind.AGENT})
_S = frozenset({ActorKind.SYSTEM})
_SH = frozenset({ActorKind.SYSTEM, ActorKind.HUMAN})  # aggregator, or a human override
S = TaskState

RULES: dict[TaskCommand, Rule] = {
    r.command: r
    for r in (
        Rule(TaskCommand.PROMOTE, frozenset({S.PENDING}), (S.READY,), _S, "TASK_READY"),
        Rule(TaskCommand.CLAIM, frozenset({S.READY}), (S.ASSIGNED,), _A, "TASK_ASSIGNED"),
        Rule(TaskCommand.ASSIGN, frozenset({S.READY}), (S.ASSIGNED,), _H, "TASK_ASSIGNED"),
        Rule(TaskCommand.START, frozenset({S.ASSIGNED}), (S.IN_PROGRESS,), _A, "AGENT_STARTED"),
        Rule(TaskCommand.SUBMIT, frozenset({S.IN_PROGRESS}), (S.IN_REVIEW,), _A, "TASK_SUBMITTED"),
        Rule(
            TaskCommand.ASK_HUMAN,
            frozenset({S.IN_PROGRESS}),
            (S.WAITING_FOR_HUMAN,),
            _A,
            "HUMAN_INPUT_REQUESTED",
        ),
        Rule(
            TaskCommand.ANSWER,
            frozenset({S.WAITING_FOR_HUMAN}),
            (S.READY,),
            _H,
            "HUMAN_INPUT_PROVIDED",
        ),
        Rule(
            TaskCommand.FAIL,
            EXECUTING_STATES,
            (S.READY, S.FAILED),
            _A,
            "EXECUTION_FAILED",
        ),
        Rule(
            TaskCommand.TIME_OUT,
            EXECUTING_STATES,
            (S.READY, S.FAILED),
            _S,
            "EXECUTION_TIMED_OUT",
        ),
        Rule(
            TaskCommand.APPROVE,
            frozenset({S.IN_REVIEW}),
            (S.AWAITING_MERGE,),
            _SH,
            "REVIEW_APPROVED",
        ),
        Rule(
            TaskCommand.REQUEST_CHANGES,
            frozenset({S.IN_REVIEW}),
            (S.READY,),
            _SH,
            "TASK_REOPENED",
        ),
        Rule(
            TaskCommand.ESCALATE,
            frozenset({S.IN_REVIEW}),
            (S.WAITING_FOR_HUMAN,),
            _S,
            "TASK_ESCALATED",
        ),
        Rule(
            TaskCommand.REJECT_MERGE,
            frozenset({S.AWAITING_MERGE}),
            (S.READY,),
            _H,
            "TASK_REOPENED",
        ),
        # Only sends work back, so the sync may do it. It can't merge or approve anything.
        Rule(
            TaskCommand.REPORT_CONFLICT,
            frozenset({S.IN_REVIEW, S.AWAITING_MERGE}),
            (S.READY,),
            _S,
            "TASK_REOPENED",
        ),
        # A review failed for its own reasons, such as budget. Reviews again, without new work.
        Rule(
            TaskCommand.REREVIEW,
            frozenset({S.WAITING_FOR_HUMAN}),
            (S.IN_REVIEW,),
            _H,
            "TASK_REREVIEW_REQUESTED",
        ),
        # From WAITING_FOR_HUMAN: you merged a PR Axiom had escalated to you. Its guard needs a PR.
        Rule(
            TaskCommand.MERGE,
            frozenset({S.AWAITING_MERGE, S.WAITING_FOR_HUMAN}),
            (S.DONE,),
            _H,
            "TASK_DONE",
        ),
        Rule(TaskCommand.RETRY, frozenset({S.FAILED}), (S.READY,), _H, "TASK_RETRIED"),
        Rule(TaskCommand.CANCEL, NON_TERMINAL_STATES, (S.CANCELLED,), _H, "TASK_CANCELLED"),
    )
}


class IllegalTransition(Exception):
    """The command is not legal from the task's current state."""

    def __init__(self, command: TaskCommand, state: TaskState) -> None:
        self.command = command
        self.state = state
        self.allowed = allowed_commands(state)
        allowed = ", ".join(self.allowed) or "none (terminal state)"
        super().__init__(f"cannot {command} a task in {state}; allowed: {allowed}")


class ActorNotPermitted(Exception):
    """This kind of actor may not issue this command."""

    def __init__(self, command: TaskCommand, actor_kind: ActorKind) -> None:
        self.command = command
        self.actor_kind = actor_kind
        permitted = ", ".join(sorted(RULES[command].actors))
        super().__init__(f"{actor_kind} may not {command}; permitted: {permitted}")


def allowed_commands(state: TaskState) -> list[TaskCommand]:
    return [c for c, r in RULES.items() if state in r.sources]


def check(command: TaskCommand, state: TaskState, actor_kind: ActorKind) -> Rule:
    """Return the rule if `actor_kind` may issue `command` in `state`, else raise."""
    rule = RULES[command]
    if state not in rule.sources:
        raise IllegalTransition(command, state)
    if actor_kind not in rule.actors:
        raise ActorNotPermitted(command, actor_kind)
    return rule


def next_state(
    command: TaskCommand,
    state: TaskState,
    actor_kind: ActorKind,
    *,
    attempts_after: int = 0,
    max_retries: int = 0,
    budget_exhausted: bool = False,
    credential_exposure: bool = False,
) -> TaskState:
    """Target state for a legal command.

    For retry-dependent commands (`fail`, `time_out`) pass the attempt count *after* this
    failure; the task goes back to READY while attempts_after <= max_retries and its budget is
    not exhausted, else FAILED. A possible credential exposure is never retried automatically:
    work stops, and only a human can retry it.
    """
    rule = check(command, state, actor_kind)
    if rule.retry_dependent:
        retry_target, exhausted_target = rule.targets
        can_retry = attempts_after <= max_retries and not budget_exhausted
        can_retry = can_retry and not credential_exposure
        return retry_target if can_retry else exhausted_target
    return rule.targets[0]
