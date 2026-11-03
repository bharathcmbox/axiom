"""T4: every legal transition and every illegal one, generated from the transition table,
executed against the real database."""

import itertools

import pytest

from axiom.domain.execution_machine import TRANSITIONS as EXEC_TRANSITIONS
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import (
    EXECUTING_STATES,
    RULES,
    ActorNotPermitted,
    IllegalTransition,
    TaskCommand,
    TaskState,
)
from axiom.errors import (
    AssignmentRejected,
    DependenciesNotMet,
    InvalidInput,
    LeaseExpired,
    LeaseNotExpired,
    NotExecutionOwner,
    NotFound,
)
from axiom.orchestrator import commands
from axiom.orchestrator.assignment import claim_next
from axiom.orchestrator.commands import apply_command
from axiom.orchestrator.tasks import get_task
from axiom.registry.agents import set_enabled
from axiom.state.db import Conn
from tests.integration.helpers import (
    DEV1,
    DEV2,
    HUMAN,
    REAPER,
    do,
    drive_to,
    events_for,
    expire_lease,
    snapshot,
)

C, S = TaskCommand, TaskState

# (command, source state, max_retries, expected target) for every legal row.
# promote is covered separately: it is only ever issued by the system when dependencies finish.
LEGAL: list[tuple[TaskCommand, TaskState, int, TaskState]] = []
for _cmd, _rule in RULES.items():
    if _cmd == C.PROMOTE:
        continue
    for _src in sorted(_rule.sources):
        if _rule.retry_dependent:
            LEGAL.append((_cmd, _src, 2, _rule.targets[0]))  # retries left -> READY
            LEGAL.append((_cmd, _src, 0, _rule.targets[1]))  # retries used up -> FAILED
        else:
            # A task only reaches FAILED with its retries used up (drive_to uses max_retries=0).
            LEGAL.append((_cmd, _src, 0 if _src == S.FAILED else 2, _rule.targets[0]))

ILLEGAL = [
    (cmd, state)
    for cmd, state in itertools.product(RULES, S)
    if state not in RULES[cmd].sources and cmd not in (C.CLAIM, C.PROMOTE)
]


def _id(case: tuple[object, ...]) -> str:
    return "-".join(str(x) for x in case)


def test_legal_cases_cover_every_rule() -> None:
    covered = {(c, s) for c, s, _, _ in LEGAL} | {(C.PROMOTE, S.PENDING)}
    assert covered == {(c, s) for c, r in RULES.items() for s in r.sources}


@pytest.mark.parametrize("case", LEGAL, ids=_id)
def test_legal_transition(org: Conn, case: tuple[TaskCommand, TaskState, int, TaskState]) -> None:
    command, source, max_retries, expected = case
    key = drive_to(org, source, max_retries=max_retries)
    before = events_for(org, key)

    task = do(org, key, command)

    assert task.state == expected
    new_events = events_for(org, key)[len(before) :]
    # Exactly one task transition; review bookkeeping events (Phase 3) do not move the task.
    transitions = [e for e in new_events if e["to_state"] is not None]
    assert len(transitions) == 1
    [event] = transitions
    review_types = sorted(e["type"] for e in new_events if e["review_id"] is not None)
    if command == C.SUBMIT:
        assert review_types == ["REVIEW_REQUESTED", "REVIEW_REQUESTED"]  # code + security
    elif source == S.IN_REVIEW:
        assert review_types == ["REVIEW_SUPERSEDED", "REVIEW_SUPERSEDED"]
    else:
        assert review_types == []
    assert (event["type"], event["from_state"], event["to_state"]) == (
        RULES[command].event, source, expected,
    )  # fmt: skip
    assert event["payload"]["command"] == command
    # Executions follow the task: created on claim/assign, moved by commands on executing tasks.
    executions = org.execute(
        "SELECT * FROM executions WHERE task_id = %s ORDER BY id", (task.id,)
    ).fetchall()
    if command in (C.CLAIM, C.ASSIGN):
        assert executions[-1]["status"] == "STARTING"
        assert event["execution_id"] == executions[-1]["id"]
    elif command in EXEC_TRANSITIONS and source in EXECUTING_STATES:
        assert executions[-1]["status"] == EXEC_TRANSITIONS[command][1]
        assert event["execution_id"] == executions[-1]["id"]


@pytest.mark.parametrize("case", ILLEGAL, ids=_id)
def test_illegal_transition_rejected(org: Conn, case: tuple[TaskCommand, TaskState]) -> None:
    command, state = case
    key = drive_to(org, state)
    before = snapshot(org, key)

    with pytest.raises(IllegalTransition) as exc:
        do(org, key, command)

    assert exc.value.state == state
    assert snapshot(org, key) == before


@pytest.mark.parametrize("state", [s for s in S if s != S.READY], ids=str)
def test_claim_ignores_tasks_that_are_not_ready(org: Conn, state: TaskState) -> None:
    key = drive_to(org, state)
    for t in org.execute("SELECT key FROM tasks WHERE state = 'READY'").fetchall():
        do(org, t["key"], C.CANCEL)  # e.g. the dependency created for PENDING
    before = snapshot(org, key)
    assert claim_next(org, DEV1) is None
    assert snapshot(org, key) == before


def test_claim_cannot_target_a_specific_task(org: Conn) -> None:
    key = drive_to(org, S.READY)
    with pytest.raises(InvalidInput):
        apply_command(org, DEV1, key, C.CLAIM)


ACTOR_OF_KIND = {
    ActorKind.HUMAN: HUMAN,
    ActorKind.AGENT: DEV1,
    ActorKind.SYSTEM: Actor(ActorKind.SYSTEM, "x"),
}
WRONG_ACTOR = [
    (cmd, kind)
    for cmd, rule in RULES.items()
    for kind in ActorKind
    if kind not in rule.actors and cmd not in (C.CLAIM, C.PROMOTE)
]


@pytest.mark.parametrize("case", WRONG_ACTOR, ids=_id)
def test_wrong_actor_kind_rejected(org: Conn, case: tuple[TaskCommand, ActorKind]) -> None:
    command, kind = case
    source = sorted(RULES[command].sources)[0]
    key = drive_to(org, source)
    if command == C.TIME_OUT:
        expire_lease(org, key)  # so that only the actor kind makes it illegal
    before = snapshot(org, key)
    with pytest.raises(ActorNotPermitted):
        do(org, key, command, actor=ACTOR_OF_KIND[kind])
    assert snapshot(org, key) == before


def test_humans_cannot_claim_and_agents_cannot_promote(org: Conn) -> None:
    with pytest.raises(ActorNotPermitted):
        claim_next(org, HUMAN)
    key = drive_to(org, S.PENDING)
    with pytest.raises(ActorNotPermitted):
        apply_command(org, DEV1, key, C.PROMOTE)


# ---- guards -------------------------------------------------------------------------------


def test_promote_requires_dependencies_done(org: Conn) -> None:
    key = drive_to(org, S.PENDING)
    before = snapshot(org, key)
    with pytest.raises(DependenciesNotMet):
        do(org, key, C.PROMOTE)
    assert snapshot(org, key) == before


def test_only_the_owning_agent_can_act_on_an_execution(org: Conn) -> None:
    key = drive_to(org, S.ASSIGNED)  # assigned to DEV-01
    before = snapshot(org, key)
    for command in (C.START, C.FAIL):
        with pytest.raises(NotExecutionOwner):
            do(org, key, command, actor=DEV2)
    assert snapshot(org, key) == before


@pytest.mark.parametrize("command", [C.START, C.FAIL])
def test_agent_commands_rejected_after_lease_expiry(org: Conn, command: TaskCommand) -> None:
    key = drive_to(org, S.ASSIGNED)
    expire_lease(org, key)
    before = snapshot(org, key)
    with pytest.raises(LeaseExpired):
        do(org, key, command)
    assert snapshot(org, key) == before


def test_submit_rejected_after_lease_expiry(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    expire_lease(org, key)
    before = snapshot(org, key)
    with pytest.raises(LeaseExpired):
        do(org, key, C.SUBMIT)
    assert snapshot(org, key) == before


def test_time_out_requires_an_expired_lease(org: Conn) -> None:
    key = drive_to(org, S.IN_PROGRESS)
    before = snapshot(org, key)
    with pytest.raises(LeaseNotExpired):
        apply_command(org, REAPER, key, C.TIME_OUT)
    assert snapshot(org, key) == before


def test_assign_requires_matching_role(org: Conn) -> None:
    key = drive_to(org, S.READY)  # assigned_role = developer
    before = snapshot(org, key)
    with pytest.raises(AssignmentRejected, match="code_reviewer"):
        do(org, key, C.ASSIGN, agent_key="REV-01")
    assert snapshot(org, key) == before


def test_assign_rejects_disabled_and_unknown_agents(org: Conn) -> None:
    key = drive_to(org, S.READY)
    set_enabled(org, HUMAN, "DEV-01", False)
    before = snapshot(org, key)
    with pytest.raises(AssignmentRejected, match="disabled"):
        do(org, key, C.ASSIGN, agent_key="DEV-01")
    with pytest.raises(NotFound):
        do(org, key, C.ASSIGN, agent_key="DEV-99")
    with pytest.raises(InvalidInput):
        apply_command(org, HUMAN, key, C.ASSIGN)
    assert snapshot(org, key) == before


def test_disabled_agent_cannot_claim(org: Conn) -> None:
    key = drive_to(org, S.READY)
    set_enabled(org, HUMAN, "DEV-01", False)
    before = snapshot(org, key)
    with pytest.raises(AssignmentRejected):
        claim_next(org, DEV1)
    assert snapshot(org, key) == before


# ---- atomicity ----------------------------------------------------------------------------


def test_event_write_failure_rolls_back_the_transition(
    org: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = drive_to(org, S.READY)
    before = snapshot(org, key)

    def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(commands, "append_event", boom)
    with pytest.raises(RuntimeError):
        do(org, key, C.ASSIGN)
    assert snapshot(org, key) == before  # no state change, no execution row
    assert get_task(org, key).state == S.READY
