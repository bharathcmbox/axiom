# State machines

Axiom has two state machines:

- **Task**: where a unit of work is in the delivery pipeline.
- **Execution**: one agent run on a task. A task can have many executions over its life (retries,
  rework), but at most one active execution at a time.

The code is the source of truth: `src/axiom/domain/task_machine.py` and
`src/axiom/domain/execution_machine.py`. Unit tests check that the tables in this document and the
code agree.

## Task

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING: create
    PENDING --> READY: promote<br/>(all deps DONE)
    READY --> ASSIGNED: claim / assign
    ASSIGNED --> IN_PROGRESS: start
    IN_PROGRESS --> IN_REVIEW: submit
    IN_PROGRESS --> WAITING_FOR_HUMAN: ask_human
    WAITING_FOR_HUMAN --> READY: answer
    ASSIGNED --> READY: fail / time_out<br/>(retries left)
    IN_PROGRESS --> READY: fail / time_out<br/>(retries left)
    ASSIGNED --> FAILED: fail / time_out<br/>(retries used up)
    IN_PROGRESS --> FAILED: fail / time_out<br/>(retries used up)
    FAILED --> READY: retry (human)
    IN_REVIEW --> AWAITING_MERGE: approve
    IN_REVIEW --> READY: request_changes
    AWAITING_MERGE --> READY: reject_merge (human)
    AWAITING_MERGE --> DONE: merge (human)
    DONE --> [*]
    CANCELLED --> [*]
```

`cancel` (human) is legal from every non-terminal state. It is left out of the diagram above to
keep it readable:

```mermaid
stateDiagram-v2
    direction LR
    state "any non-terminal state<br/>PENDING · READY · ASSIGNED · IN_PROGRESS · WAITING_FOR_HUMAN<br/>IN_REVIEW · AWAITING_MERGE · FAILED" as NT
    NT --> CANCELLED: cancel (human)
```

Terminal states: **DONE**, **CANCELLED**. FAILED is not terminal; a human can `retry` or `cancel`.

### Transition table

| # | Command → event | From | To | Actor | Guard | Side effects |
|---|---|---|---|---|---|---|
| 1 | `create` → TASK_CREATED | — | PENDING | human | dependencies exist, same project | then #2 |
| 2 | `promote` → TASK_READY | PENDING | READY | system | every dependency DONE | — |
| 3 | `claim` / `assign` → TASK_ASSIGNED | READY | ASSIGNED | agent (pull) / human (push) | agent enabled; role = `assigned_role`; agent under `max_concurrent`; global developer cap | execution created in STARTING with a lease |
| 4 | `start` → AGENT_STARTED | ASSIGNED | IN_PROGRESS | agent | owns execution, lease live | execution → RUNNING |
| 5 | `submit` → TASK_SUBMITTED | IN_PROGRESS | IN_REVIEW | agent | lease live | execution → COMPLETED; result stored |
| 6 | `ask_human` → HUMAN_INPUT_REQUESTED | IN_PROGRESS | WAITING_FOR_HUMAN | agent | lease live | execution → COMPLETED; question stored |
| 7 | `answer` → HUMAN_INPUT_PROVIDED | WAITING_FOR_HUMAN | READY | human | — | answer stored |
| 8 | `fail` → EXECUTION_FAILED | ASSIGNED, IN_PROGRESS | READY / FAILED | agent | lease live | execution → FAILED; `attempt_count += 1` |
| 9 | `time_out` → EXECUTION_TIMED_OUT | ASSIGNED, IN_PROGRESS | READY / FAILED | system:reaper | lease expired (DB clock) | execution → TIMED_OUT; `attempt_count += 1` |
| 10 | `approve` → REVIEW_APPROVED | IN_REVIEW | AWAITING_MERGE | reviewer / human | — (CI gate in Phase 3) | — |
| 11 | `request_changes` → TASK_REOPENED | IN_REVIEW | READY | reviewer / human | — | `review_rounds += 1`; findings stored |
| 12 | `reject_merge` → TASK_REOPENED | AWAITING_MERGE | READY | human | — | `review_rounds += 1` |
| 13 | `merge` → TASK_DONE | AWAITING_MERGE | DONE | human | — | #2 on dependents |
| 14 | `retry` → TASK_RETRIED | FAILED | READY | human | — | `attempt_count = 0` |
| 15 | `cancel` → TASK_CANCELLED | any non-terminal | CANCELLED | human | — | active execution → CANCELLED |

For `fail` and `time_out`, the target is READY while `attempt_count` (after the increment) is
≤ `max_retries` **and** the task's budget is not exhausted (summed execution cost < `budget_usd`);
otherwise FAILED. With the default `max_retries = 2`, a task gets 3 attempts. A human can raise
the budget (`POST /tasks/{key}/budget`) and then `retry`.

`claim` / `assign` are also refused (409 `assignment_rejected`) when the task's budget or the
daily budget is spent.

## Execution

```mermaid
stateDiagram-v2
    direction LR
    [*] --> STARTING: task claim / assign
    STARTING --> RUNNING: task start
    RUNNING --> COMPLETED: task submit / ask_human
    STARTING --> FAILED: task fail
    RUNNING --> FAILED: task fail
    STARTING --> TIMED_OUT: task time_out
    RUNNING --> TIMED_OUT: task time_out
    STARTING --> CANCELLED: task cancel
    RUNNING --> CANCELLED: task cancel
    COMPLETED --> [*]
    FAILED --> [*]
    TIMED_OUT --> [*]
    CANCELLED --> [*]
```

| Execution transition | From | To | Caused by task command |
|---|---|---|---|
| create | — | STARTING | `claim` / `assign` |
| start | STARTING | RUNNING | `start` |
| complete | RUNNING | COMPLETED | `submit`, `ask_human` |
| fail | STARTING, RUNNING | FAILED | `fail` |
| time_out | STARTING, RUNNING | TIMED_OUT | `time_out` |
| cancel | STARTING, RUNNING | CANCELLED | `cancel` |

Active states: STARTING, RUNNING. Only active executions hold a lease. Heartbeats extend the
lease and write no event.

## Invariants

- A task is ASSIGNED or IN_PROGRESS **iff** it has exactly one active execution.
- An execution has a lease **iff** it is active (DB CHECK constraint).
- At most one active execution per task (DB partial unique index).
- Every state change writes at least one event, in the same transaction.
