"""Execution state machine. Executions change only as side effects of task commands."""

from enum import StrEnum

from axiom.domain.task_machine import TaskCommand


class ExecutionState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


ACTIVE_STATES = frozenset({ExecutionState.STARTING, ExecutionState.RUNNING})
TERMINAL_STATES = frozenset(set(ExecutionState) - ACTIVE_STATES)
INITIAL_STATE = ExecutionState.STARTING

E = ExecutionState

# Task command -> (allowed source states, target state). `claim`/`assign` create an execution
# in INITIAL_STATE instead and are listed in CREATED_BY.
TRANSITIONS: dict[TaskCommand, tuple[frozenset[ExecutionState], ExecutionState]] = {
    TaskCommand.START: (frozenset({E.STARTING}), E.RUNNING),
    TaskCommand.SUBMIT: (frozenset({E.RUNNING}), E.COMPLETED),
    TaskCommand.ASK_HUMAN: (frozenset({E.RUNNING}), E.COMPLETED),
    TaskCommand.FAIL: (ACTIVE_STATES, E.FAILED),
    TaskCommand.TIME_OUT: (ACTIVE_STATES, E.TIMED_OUT),
    TaskCommand.CANCEL: (ACTIVE_STATES, E.CANCELLED),
}
CREATED_BY = frozenset({TaskCommand.CLAIM, TaskCommand.ASSIGN})


class IllegalExecutionTransition(Exception):
    def __init__(self, command: TaskCommand, state: ExecutionState) -> None:
        self.command = command
        self.state = state
        super().__init__(f"task command {command} cannot move an execution in {state}")


def next_execution_state(command: TaskCommand, state: ExecutionState) -> ExecutionState:
    if command not in TRANSITIONS:
        raise IllegalExecutionTransition(command, state)
    sources, target = TRANSITIONS[command]
    if state not in sources:
        raise IllegalExecutionTransition(command, state)
    return target
