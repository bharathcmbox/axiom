"""T2: the execution state machine and its coupling to task commands."""

import itertools

import pytest

from axiom.domain.execution_machine import (
    ACTIVE_STATES,
    CREATED_BY,
    INITIAL_STATE,
    TERMINAL_STATES,
    TRANSITIONS,
    ExecutionState,
    IllegalExecutionTransition,
    next_execution_state,
)
from axiom.domain.task_machine import EXECUTING_STATES, RULES, TaskCommand
from tests.unit.doc_tables import execution_rows

DOC_ROWS = execution_rows()


def test_doc_table_matches_code() -> None:
    assert len(DOC_ROWS) == 6
    created = [r for r in DOC_ROWS if r[0] == "create"]
    assert created == [("create", frozenset(), str(INITIAL_STATE), ("claim", "assign"))]
    assert set(created[0][3]) == {str(c) for c in CREATED_BY}
    documented: dict[str, tuple[frozenset[str], str]] = {}
    for _, sources, target, commands in DOC_ROWS:
        if sources:
            for cmd in commands:
                documented[cmd] = (sources, target)
    in_code = {
        str(cmd): (frozenset(str(s) for s in sources), str(target))
        for cmd, (sources, target) in TRANSITIONS.items()
    }
    assert in_code == documented


@pytest.mark.parametrize(
    ("command", "state"), list(itertools.product(TaskCommand, ExecutionState)), ids=str
)
def test_every_command_state_pair(command: TaskCommand, state: ExecutionState) -> None:
    if command in TRANSITIONS and state in TRANSITIONS[command][0]:
        assert next_execution_state(command, state) == TRANSITIONS[command][1]
    else:
        with pytest.raises(IllegalExecutionTransition):
            next_execution_state(command, state)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        for command in TaskCommand:
            with pytest.raises(IllegalExecutionTransition):
                next_execution_state(command, state)


def test_every_state_reachable() -> None:
    reached = {INITIAL_STATE} | {target for _, target in TRANSITIONS.values()}
    assert reached == set(ExecutionState)


def test_active_states() -> None:
    assert {ExecutionState.STARTING, ExecutionState.RUNNING} == ACTIVE_STATES


def test_every_task_command_on_an_executing_task_moves_its_execution() -> None:
    """Commands that leave or progress ASSIGNED/IN_PROGRESS must update the execution."""
    for command, rule in RULES.items():
        if rule.sources & EXECUTING_STATES:
            assert command in TRANSITIONS, command


def test_commands_entering_executing_states_create_executions() -> None:
    for command, rule in RULES.items():
        enters = set(rule.targets) & EXECUTING_STATES and not rule.sources & EXECUTING_STATES
        assert bool(enters) == (command in CREATED_BY), command
