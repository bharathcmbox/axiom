# 2. The life of a task

The best way to understand Axiom is to follow one task from creation to DONE. In Phase 1 you
play every part: the human, the developer agent and the reviewer. From Phase 2, real agents
will make the agent calls.

## The journey at a glance

```mermaid
flowchart LR
    A(["create<br/>(human)"]) --> P[PENDING]
    P -->|"promote (system,<br/>deps all DONE)"| R[READY]
    R -->|"claim (agent)"| AS[ASSIGNED]
    AS -->|"start (agent)"| IP[IN_PROGRESS]
    IP -->|"submit (agent)"| IR[IN_REVIEW]
    IR -->|"approve (reviewer/human)"| AM[AWAITING_MERGE]
    AM -->|"merge (human only)"| D([DONE])
```

The task never "decides" to move. Every arrow is a **command** issued by an **actor**, and
the command is checked against the rules before anything changes.

## Step by step

```mermaid
sequenceDiagram
    autonumber
    actor You as You (human:bharath)
    actor Dev as DEV-01 (agent)
    participant API as Axiom API
    participant DB as PostgreSQL

    You->>API: POST /projects/AX/tasks {title, specification, criteria}
    API->>DB: INSERT task (PENDING) + event TASK_CREATED
    API->>DB: no dependencies → UPDATE to READY + event TASK_READY
    API-->>You: 201 AX-001, state READY

    Dev->>API: POST /claims
    API->>DB: pick oldest highest-priority READY developer task (SKIP LOCKED)
    API->>DB: INSERT execution (STARTING, lease 300s) + task ASSIGNED + event TASK_ASSIGNED
    API-->>Dev: 200 AX-001, state ASSIGNED

    Dev->>API: POST /tasks/AX-001/commands/start
    API->>DB: execution RUNNING, task IN_PROGRESS, event AGENT_STARTED
    API-->>Dev: 200 IN_PROGRESS

    loop while working
        Dev->>API: POST /tasks/AX-001/heartbeat
        API->>DB: lease_expires_at = now() + 300s (no event)
    end

    Dev->>API: POST .../commands/submit {payload: result contract}
    API->>DB: execution COMPLETED (result stored), task IN_REVIEW, event TASK_SUBMITTED
    API-->>Dev: 200 IN_REVIEW

    You->>API: POST .../commands/approve
    API->>DB: task AWAITING_MERGE, event REVIEW_APPROVED
    You->>API: POST .../commands/merge
    API->>DB: task DONE, event TASK_DONE, promote any dependents
    API-->>You: 200 DONE
```

## What the database looks like afterwards

**`tasks`**: one row, `state = DONE`, `attempt_count = 0`, `review_rounds = 0`.

**`executions`**: one row, the developer's single attempt:

| id | task | agent | attempt | status | lease_expires_at | ended_at | result |
|---|---|---|---|---|---|---|---|
| 1 | AX-001 | DEV-01 | 1 | COMPLETED | *null* | 10:07 | `{"summary": "done", ...}` |

**`events`**: seven rows. Together they are the complete history:

| id | type | from → to | actor |
|---|---|---|---|
| 1 | TASK_CREATED | — → PENDING | human:bharath |
| 2 | TASK_READY | PENDING → READY | system:dependencies |
| 3 | TASK_ASSIGNED | READY → ASSIGNED | agent:DEV-01 |
| 4 | AGENT_STARTED | ASSIGNED → IN_PROGRESS | agent:DEV-01 |
| 5 | TASK_SUBMITTED | IN_PROGRESS → IN_REVIEW | agent:DEV-01 |
| 6 | REVIEW_APPROVED | IN_REVIEW → AWAITING_MERGE | human:bharath |
| 7 | TASK_DONE | AWAITING_MERGE → DONE | human:bharath |

Notice that the `to` of each event is the `from` of the next. That chain is what lets Axiom
(and you, later, for metrics) reconstruct exactly what happened. The test suite checks the
chain after every test (chapter 9).

## A task with a dependency

```mermaid
sequenceDiagram
    actor You
    participant API
    participant DB as PostgreSQL
    You->>API: create AX-001 (no deps)
    API-->>You: AX-001 READY
    You->>API: create AX-002 depends on AX-001
    API-->>You: AX-002 PENDING
    Note over DB: ...AX-001 is claimed, built, reviewed...
    You->>API: merge AX-001
    API->>DB: AX-001 → DONE
    API->>DB: find PENDING dependents of AX-001 → AX-002
    API->>DB: all of AX-002's deps DONE? yes → AX-002 READY (actor system:dependencies)
    API-->>You: AX-001 DONE
```

The promotion happens **inside the same transaction as the merge**. There is never a moment
where AX-001 is DONE but AX-002 is still waiting for a background job to notice.

## When things go wrong

The happy path is only part of the story. These are the detours a task can take; chapter 3
covers them in detail.

```mermaid
flowchart TB
    IP[IN_PROGRESS] -->|"fail / lease expired<br/>retries left"| R[READY]
    IP -->|"fail / lease expired<br/>retries used up"| F[FAILED]
    F -->|"retry (human)"| R
    IP -->|ask_human| W[WAITING_FOR_HUMAN]
    W -->|"answer (human)"| R
    IR[IN_REVIEW] -->|request_changes| R
    AM[AWAITING_MERGE] -->|"reject_merge (human)"| R
```

Every detour ends at READY. The task goes back to the queue and any agent of the right role
can pick it up. A new execution is created each time, so the history of attempts is kept.

## Try it

With the stack running (`make up`), paste the walkthrough from
[api.md](../api.md#walkthrough-one-task-from-creation-to-done-by-hand) one line at a time.
After each line, run:

```bash
curl -s "${H[@]}" localhost:8000/tasks/AX-001 | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["state"])'
curl -s "${H[@]}" localhost:8000/tasks/AX-001/events | python3 -c 'import json,sys; [print(e["id"], e["type"], e["from_state"], "->", e["to_state"]) for e in json.load(sys.stdin)]'
```

Then try to break it:
- send `merge` twice (409, with the allowed commands);
- `start` with DEV-02's token (403, not the owner);
- `cancel` as an agent (403, only humans cancel).

Each time, confirm the event list did not grow.
