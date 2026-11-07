"""T2: the task transition table, checked exhaustively against docs/state-machines.md."""

import itertools
from collections import deque

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import (
    EXECUTING_STATES,
    INITIAL_STATE,
    NON_TERMINAL_STATES,
    RULES,
    TERMINAL_STATES,
    ActorNotPermitted,
    IllegalTransition,
    TaskCommand,
    TaskState,
    allowed_commands,
    check,
    next_state,
)
from tests.unit.doc_tables import task_rows

DOC_ROWS = task_rows()
# command -> (sources, targets, event) as documented
DOC: dict[str, tuple[frozenset[str], tuple[str, ...], str]] = {
    cmd: (row.sources, row.targets, row.event) for row in DOC_ROWS for cmd in row.commands
}


def test_doc_table_was_parsed() -> None:
    assert len(DOC_ROWS) == 17
    assert DOC["create"] == (frozenset(), ("PENDING",), "TASK_CREATED")


def test_code_and_doc_list_the_same_commands() -> None:
    assert set(RULES) == set(DOC) - {"create"}


@pytest.mark.parametrize("command", list(TaskCommand))
def test_rule_matches_doc(command: TaskCommand) -> None:
    sources, targets, event = DOC[command]
    rule = RULES[command]
    assert {str(s) for s in rule.sources} == sources
    assert tuple(str(t) for t in rule.targets) == targets
    assert rule.event == event


def _permitted_actor(command: TaskCommand) -> ActorKind:
    return sorted(RULES[command].actors)[0]


@pytest.mark.parametrize(
    ("command", "state"), list(itertools.product(TaskCommand, TaskState)), ids=str
)
def test_every_command_state_pair(command: TaskCommand, state: TaskState) -> None:
    """Legal iff the doc lists `state` as a source of `command`; illegal pairs raise."""
    actor = _permitted_actor(command)
    if state in DOC[command][0]:
        assert check(command, state, actor) is RULES[command]
    else:
        with pytest.raises(IllegalTransition) as exc:
            check(command, state, actor)
        assert exc.value.state == state
        assert command not in exc.value.allowed


@pytest.mark.parametrize(
    ("command", "kind"), list(itertools.product(TaskCommand, ActorKind)), ids=str
)
def test_actor_kinds(command: TaskCommand, kind: ActorKind) -> None:
    state = next(iter(RULES[command].sources))
    if kind in RULES[command].actors:
        check(command, state, kind)
    else:
        with pytest.raises(ActorNotPermitted):
            check(command, state, kind)


def test_only_humans_merge_retry_cancel_answer_and_reject() -> None:
    for cmd in ("merge", "retry", "cancel", "answer", "reject_merge", "assign"):
        assert RULES[TaskCommand(cmd)].actors == {ActorKind.HUMAN}


def test_agents_cannot_merge_their_own_work() -> None:
    with pytest.raises(ActorNotPermitted):
        check(TaskCommand.MERGE, TaskState.AWAITING_MERGE, ActorKind.AGENT)


def test_terminal_states_have_no_exits() -> None:
    assert {TaskState.DONE, TaskState.CANCELLED} == TERMINAL_STATES
    for state in TERMINAL_STATES:
        assert allowed_commands(state) == []


def test_every_non_terminal_state_can_be_cancelled() -> None:
    for state in NON_TERMINAL_STATES:
        assert TaskCommand.CANCEL in allowed_commands(state)


def test_every_state_is_reachable_from_initial() -> None:
    seen = {INITIAL_STATE}
    queue = deque([INITIAL_STATE])
    while queue:
        state = queue.popleft()
        for cmd in allowed_commands(state):
            for target in RULES[cmd].targets:
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
    assert seen == set(TaskState)


def test_every_non_terminal_state_can_reach_done() -> None:
    for start in NON_TERMINAL_STATES:
        seen = {start}
        queue = deque([start])
        while queue:
            state = queue.popleft()
            for cmd in allowed_commands(state):
                for target in RULES[cmd].targets:
                    if target not in seen:
                        seen.add(target)
                        queue.append(target)
        assert TaskState.DONE in seen, start


@pytest.mark.parametrize(
    ("attempts_after", "max_retries", "expected"),
    [
        (1, 2, TaskState.READY),
        (2, 2, TaskState.READY),
        (3, 2, TaskState.FAILED),  # default: 3 attempts total
        (1, 0, TaskState.FAILED),  # max_retries=0: no retries
    ],
)
@pytest.mark.parametrize("command", [TaskCommand.FAIL, TaskCommand.TIME_OUT])
def test_retry_dependent_targets(
    command: TaskCommand, attempts_after: int, max_retries: int, expected: TaskState
) -> None:
    actor = _permitted_actor(command)
    got = next_state(
        command,
        TaskState.IN_PROGRESS,
        actor,
        attempts_after=attempts_after,
        max_retries=max_retries,
    )
    assert got == expected


def test_only_fail_and_time_out_are_retry_dependent() -> None:
    assert {c for c, r in RULES.items() if r.retry_dependent} == {
        TaskCommand.FAIL,
        TaskCommand.TIME_OUT,
    }


def test_executing_states_are_exactly_where_executions_live() -> None:
    assert {TaskState.ASSIGNED, TaskState.IN_PROGRESS} == EXECUTING_STATES


def test_illegal_transition_message_lists_allowed_commands() -> None:
    allowed = "allowed: approve, request_changes, escalate, report_conflict, cancel"
    with pytest.raises(IllegalTransition, match=allowed):
        check(TaskCommand.MERGE, TaskState.IN_REVIEW, ActorKind.HUMAN)
    with pytest.raises(IllegalTransition, match="none \\(terminal state\\)"):
        check(TaskCommand.CANCEL, TaskState.DONE, ActorKind.HUMAN)


@pytest.mark.parametrize(
    ("raw", "kind", "name"),
    [("human:bharath", "human", "bharath"), ("agent:DEV-01", "agent", "DEV-01"),
     ("system:reaper", "system", "reaper")],
)  # fmt: skip
def test_actor_parse(raw: str, kind: str, name: str) -> None:
    actor = Actor.parse(raw)
    assert (actor.kind, actor.name, str(actor)) == (kind, name, raw)


@pytest.mark.parametrize("raw", ["", "bharath", "human:", "robot:x", ":x"])
def test_actor_parse_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        Actor.parse(raw)


@pytest.mark.parametrize("command", [TaskCommand.FAIL, TaskCommand.TIME_OUT])
def test_exhausted_budget_fails_even_with_retries_left(command: TaskCommand) -> None:
    actor = _permitted_actor(command)
    got = next_state(
        command, TaskState.IN_PROGRESS, actor, attempts_after=1, max_retries=2,
        budget_exhausted=True,
    )  # fmt: skip
    assert got == TaskState.FAILED
