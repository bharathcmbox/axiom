# 5. The command engine

There is exactly **one** function that changes the state of a task or an execution:
`apply_locked` in [`orchestrator/commands.py`](../../src/axiom/orchestrator/commands.py).
Every route, the reaper, `claim_next` and dependency promotion all go through it. If you
understand this function, you understand Axiom's core.

## Who calls it

```mermaid
flowchart LR
    r1["POST /tasks/{key}/commands/{cmd}"] --> ac["apply_command()<br/>opens transaction,<br/>locks task by key"]
    r2["POST /claims"] --> cn["claim_next()<br/>opens transaction,<br/>locks a READY task<br/>with SKIP LOCKED"]
    rp["reaper thread"] --> el["expire_leases()<br/>one transaction per task,<br/>SKIP LOCKED"]
    ct["create_task()"] --> pr["promote_if_ready()"]
    al["apply_locked()<br/>THE engine"]
    ac --> al
    cn --> al
    el --> al
    pr --> al
    al -->|"on merge"| pr
```

The callers differ only in **how they find and lock the task row**. After that, they all
hand the locked row to `apply_locked`.

## What happens inside, step by step

```mermaid
flowchart TB
    start(["apply_locked(row, command, actor)"]) --> chk["1. check(command, state, actor.kind)<br/>against the transition table"]
    chk -->|illegal| x1["IllegalTransition / ActorNotPermitted<br/>(nothing written)"]
    chk -->|legal| g["2. guards for this command"]
    g --> gp{"promote?"}
    gp -->|"deps not all DONE"| x2[DependenciesNotMet]
    g --> gc{"claim / assign?"}
    gc --> ra["resolve agent: exists, enabled,<br/>role matches, under max_concurrent,<br/>under global developer cap"]
    ra -->|fails| x3["AssignmentRejected / NotFound"]
    ra -->|ok| ce["INSERT execution (STARTING, lease)"]
    g --> ge{"command on an<br/>executing task?"}
    ge --> le["lock the active execution"]
    le --> og["agent must own it, lease must be live;<br/>time_out needs an expired lease"]
    og -->|fails| x4["NotExecutionOwner / LeaseExpired /<br/>LeaseNotExpired"]
    og -->|ok| ue["UPDATE execution status<br/>(per execution_machine)"]
    ce --> cnt
    ue --> cnt
    gp -->|ok| cnt
    g --> cnt["3. counters: fail/time_out → attempt_count+1<br/>request_changes/reject_merge → review_rounds+1<br/>retry → attempt_count = 0"]
    cnt --> tgt["4. next_state(): target state<br/>(READY or FAILED for fail/time_out)"]
    tgt --> up["5. UPDATE tasks SET state, counters"]
    up --> ev["6. append_event(type, from, to, actor, payload)"]
    ev --> mg{"merge?"}
    mg -->|yes| pd["7. promote_if_ready() for each PENDING dependent"]
    mg -->|no| done(["return: caller commits"])
    pd --> done
```

Every arrow that ends in an error **raises an exception before anything is committed**. The
caller's `with conn.transaction():` block then rolls back, so a rejected command leaves no
trace: no state change, no execution row, no event.

## The transaction, as the database sees it

```mermaid
sequenceDiagram
    participant C as Caller
    participant E as apply_locked
    participant DB as PostgreSQL
    C->>DB: BEGIN
    C->>DB: SELECT * FROM tasks WHERE key='AX-001' FOR UPDATE
    Note right of DB: row locked until COMMIT/ROLLBACK
    C->>E: apply_locked(row, SUBMIT, agent:DEV-01)
    E->>E: check table → legal
    E->>DB: SELECT active execution ... FOR UPDATE OF e
    E->>E: owner = DEV-01 ✓  lease live ✓
    E->>DB: UPDATE executions SET status='COMPLETED', ended_at=now(), lease=NULL, result=...
    E->>DB: UPDATE tasks SET state='IN_REVIEW'
    E->>DB: INSERT INTO events (TASK_SUBMITTED, IN_PROGRESS→IN_REVIEW, ...)
    E-->>C: ok
    C->>DB: COMMIT
    Note right of DB: all three writes become visible together
```

**Atomicity** is the key property: the three writes (execution, task, event) become visible
together or not at all. A test proves it by making `append_event` throw: the task stays READY
and no execution row appears (`test_event_write_failure_rolls_back_the_transition`).

## Why "one function" matters so much

```mermaid
flowchart LR
    subgraph bad["If every route updated state itself"]
        b1[route A] --> s1[(tasks)]
        b2[route B] --> s1
        b3[reaper] --> s1
        b4[promotion] --> s1
        n1["4 places to get locking,<br/>guards and events right"]
    end
    subgraph good["Axiom"]
        g1[route A] --> al[apply_locked]
        g2[route B] --> al
        g3[reaper] --> al
        g4[promotion] --> al
        al --> s2[(tasks)]
        n2["1 place; tests generated<br/>from the table cover it"]
    end
```

Because there is a single path, "every transition writes an event" is true by construction.
It doesn't depend on remembering to do it in each feature.

## Side effects per command

| Command | Execution side effect | Task counters | Other |
|---|---|---|---|
| `claim`, `assign` | create (STARTING, lease) | — | capacity checks |
| `start` | STARTING → RUNNING, lease renewed | — | |
| `submit` | RUNNING → COMPLETED, `result` stored | — | |
| `ask_human` | RUNNING → COMPLETED, question stored | — | |
| `fail` | → FAILED, `error` stored | `attempt_count + 1` | target READY or FAILED |
| `time_out` | → TIMED_OUT, `error = "lease expired"` | `attempt_count + 1` | target READY or FAILED |
| `request_changes`, `reject_merge` | — | `review_rounds + 1` | findings in event payload |
| `retry` | — | `attempt_count = 0` | |
| `cancel` | active one → CANCELLED | — | |
| `merge` | — | — | promote dependents |
| `promote`, `answer`, `approve` | — | — | |

## Try it

1. Open `orchestrator/commands.py` and find each numbered step of the flowchart in
   `apply_locked`.
2. Set a breakpoint (or add a `print`) in `apply_locked`, run
   `uv run pytest tests/integration/test_tasks.py::test_full_happy_path_event_history -s`,
   and watch it get called once per transition.
3. Read `test_event_write_failure_rolls_back_the_transition` in
   `tests/integration/test_transitions.py`. It is eight lines and proves atomicity.
