# 9. How it is tested

1004 tests run in about 90 seconds against a real PostgreSQL, plus 9 Docker tests against the real
sandbox image and 2 live tests that call the real API. This chapter explains how they
are organized, and the three techniques that give confidence beyond "the happy path works".

## The shape of the suite

```mermaid
flowchart TB
    subgraph unit["tests/unit: pure, no database (535 tests, about a second)"]
        u1["test_task_machine.py<br/>every command × state, every command × actor kind,<br/>reachability, terminal states"]
        u2["test_execution_machine.py<br/>every command × execution state"]
        u3["doc_tables.py<br/>parses docs/state-machines.md"]
    end
    subgraph integ["tests/integration: real Postgres (468 tests)"]
        i1["test_migrations.py: schema + constraints"]
        i2["test_registry.py: projects, agents"]
        i3["test_transitions.py: generated legal/illegal/actor cases"]
        i4["test_tasks.py: creation, dependencies, retries, payloads"]
        i5["test_leases.py: races, caps, heartbeats, reaper"]
        i6["test_api.py: HTTP"]
        i7["test_restart.py: durability"]
        i8["test_invariants.py: the checker catches corruption"]
        i9["test_budgets, test_tokens: money and auth"]
        i10["test_workspaces, test_verify, test_publish: git + checks"]
        i11["test_worker, test_review_worker: harnesses over HTTP"]
        i12["test_reviews, test_sync: reviews, aggregator, GitHub"]
        i13["test_full_loop: every role together"]
    end
    u3 --> u1
    u3 --> u2
```

**Why a real database and no mocks?** The things most likely to be wrong are exactly what a
mock would fake away: `SKIP LOCKED`, row locks, constraints, transaction rollback. The tests
use a separate `axiom_test` database inside the same compose Postgres.

## Fixtures: a clean world for every test

```mermaid
flowchart LR
    s["session start"] --> r["reset_schema():<br/>DROP SCHEMA public CASCADE,<br/>run migrations"]
    r --> t{"each test"}
    t --> tr["TRUNCATE all tables<br/>RESTART IDENTITY"]
    tr --> org["org fixture:<br/>project AX + 7 agents"]
    org --> run["test body"]
    run --> inv["teardown: assert check_invariants() == []"]
    inv --> t
```

The teardown step means **every integration test is also an invariant test**. If any code path
ever left a task ASSIGNED without an execution, or changed state without an event, the test
that triggered it would fail, even if its own assertions passed.

## Technique 1: tests generated from the transition table

Writing tests by hand for every (command, state) pair would mean 150 tests to keep in sync.
Instead the tests **read the table** and generate themselves:

```mermaid
flowchart LR
    rules["RULES in task_machine.py"] --> legal["LEGAL cases:<br/>every (command, source state)<br/>+ both retry branches → 27"]
    rules --> illegal["ILLEGAL cases:<br/>every (command, state) NOT in sources → 108"]
    rules --> actors["WRONG_ACTOR cases:<br/>every (command, kind) NOT permitted → 24"]
    legal --> tl["drive_to(source) via real commands,<br/>apply command,<br/>assert target state + exactly 1 event<br/>+ execution side effect"]
    illegal --> ti["drive_to(state),<br/>snapshot,<br/>assert IllegalTransition,<br/>assert snapshot unchanged"]
    actors --> ta["assert ActorNotPermitted,<br/>snapshot unchanged"]
```

`drive_to(state)` is the key helper. It reaches any state using **only real commands**, never
by writing `state = 'X'` into the database. So the setup itself exercises the engine:

```mermaid
flowchart LR
    c[create] --> READY
    READY -->|assign| ASSIGNED
    ASSIGNED -->|start| IN_PROGRESS
    IN_PROGRESS -->|submit| IN_REVIEW
    IN_REVIEW -->|approve| AWAITING_MERGE
    AWAITING_MERGE -->|merge| DONE
    IN_PROGRESS -->|ask_human| WAITING_FOR_HUMAN
    IN_PROGRESS -->|"fail (max_retries=0)"| FAILED
    READY -->|cancel| CANCELLED
```

Add a new rule to the table, and new legal and illegal tests appear automatically. A guard
test (`test_legal_cases_cover_every_rule`) fails if a rule has no legal test.

## Technique 2: the documentation is tested

`tests/unit/doc_tables.py` parses the transition tables in
[docs/state-machines.md](../state-machines.md). The unit tests assert the code matches them,
row by row. The doc you read is guaranteed to describe the code you run.

## Technique 3: race tests

Concurrency bugs don't show up in sequential tests. The race tests start real threads, each
with its own database connection, and release them at the same instant with a
`threading.Barrier`:

```mermaid
sequenceDiagram
    participant T as Test
    participant W1 as Thread 1
    participant W2 as Thread 2
    participant Wn as Thread 10
    T->>W1: start (own connection)
    T->>W2: start
    T->>Wn: start
    Note over W1,Wn: all wait at barrier.wait()
    par released together
        W1->>W1: claim_next()
    and
        W2->>W2: claim_next()
    and
        Wn->>Wn: claim_next()
    end
    T->>T: join all, then assert each task claimed exactly once
```

Race tests in the suite:
- 10 agents vs 3 tasks;
- 5 developers vs the global cap of 2;
- the same agent claiming twice at once;
- two dependencies merged at once.

Each runs several rounds.

## Testing the tester

An invariant checker that never fires proves nothing. `test_invariants.py` deliberately
corrupts the database: it changes a state without an event, removes an execution, and pushes
an agent over capacity. It then asserts that `check_invariants` reports each problem.

## Beyond the default suite

```mermaid
flowchart LR
    d["make test<br/>1004 tests, free,<br/>real Postgres and git"] --> dk["make test-docker<br/>9 tests, free,<br/>real sandbox + proxy"]
    dk --> lv["make test-live<br/>2 tests, a few cents,<br/>real models"]
    lv --> run["Exit demos<br/>real repo, real PRs"]
```

Each step out found bugs the step before could not: see the list in
[chapter 14](14-how-it-was-built.md#what-went-wrong).

## Commands

```bash
make test                                    # everything (starts Postgres if needed)
uv run pytest tests/unit -q                  # pure tests only, under 1 second
uv run pytest tests/integration/test_leases.py -v
uv run pytest -k "illegal and merge" -v      # e.g. every illegal 'merge' case
make lint                                    # ruff + mypy --strict
```

## Try it

1. `uv run pytest --collect-only -q | grep "test_illegal_transition_rejected" | head` shows the
   generated case IDs, such as `merge-READY`.
2. Introduce a bug: in `apply_locked`, skip `append_event` when `command == TaskCommand.APPROVE`.
   Run `make test` and see how many different tests catch it, including the invariant teardown
   in unrelated tests. Then revert.
3. Write one new test yourself. Assert that a task in WAITING_FOR_HUMAN cannot be claimed.
   (Hint: `drive_to(org, TaskState.WAITING_FOR_HUMAN)` then `claim_next(org, DEV1) is None`.)
