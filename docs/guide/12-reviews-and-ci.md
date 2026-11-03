# 12. Independent review and the CI gate (Phase 3)

> This chapter grows with Phase 3. Each section was written in the same commit as the code it
> describes.

## The idea in one picture

In Phase 2, a developer agent's PR went to IN_REVIEW and waited for a human to `approve` it by
hand. Phase 3 puts an independent quality organization in that gap:

```mermaid
flowchart TB
    dev["developer agent<br/>Claude Opus 5.5<br/>edits files"] -->|submit| IR[IN_REVIEW]
    IR --> r1["code review<br/>REV-01, Claude Fable 5.1<br/>read-only"]
    IR --> r2["security review<br/>SEC-01, Claude Fable 5.1<br/>read-only"]
    IR --> ci["CI on GitHub Actions<br/>(tests)"]
    r1 --> agg{"aggregator<br/>(plain code)"}
    r2 --> agg
    ci --> agg
    agg -->|"a FAIL, or CI red"| back["READY again,<br/>findings as feedback"]
    agg -->|"all PASS + CI green"| AM["AWAITING_MERGE<br/>(you merge on GitHub)"]
    agg -->|"3 rounds, or a review<br/>that keeps erroring"| you["WAITING_FOR_HUMAN"]
```

Three principles, each enforced in code rather than by a prompt:

1. **Nobody approves their own work.** Reviewers are separate agents with a separate role, on
   a different model. No agent can issue `approve` or `request_changes`.
2. **A model gives an opinion; code makes the decision.** Reviewers return a verdict and
   findings. The *aggregator* (deterministic code) combines the reviews and CI into one task
   transition.
3. **Trust CI, not claims.** The developer's "tests pass" is a claim; the CI result on GitHub
   is evidence, and it is part of the gate.

## Reviews are their own kind of work (T2)

A review is not another execution of the task. It has its own table (`reviews`) and its own
state machine:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> PENDING: task submitted
    PENDING --> CLAIMED: reviewer claims (lease)
    CLAIMED --> PASS: verdict PASS
    CLAIMED --> FAIL: verdict FAIL
    CLAIMED --> PENDING: run failed / lease expired (attempts left)
    CLAIMED --> ERROR: 3 attempts used
    PENDING --> SUPERSEDED: task moved on
    CLAIMED --> SUPERSEDED: task moved on
```

Why a separate table, rather than reusing executions:
- A task has **one** developer execution at a time; it has **two** reviews at once (code and
  security). Keeping them apart preserves the Phase 1 invariant "a task is executing iff it has
  exactly one active execution".
- Each review **round** gets fresh reviews. A unique key `(task, round, role)` makes "one code
  review per round" a database guarantee.
- Reviews have their own cost, tokens and contract, so metrics like "cost of review per merged
  task" fall straight out of the table.

### How reviews relate to the task

```mermaid
flowchart LR
    subgraph task["Task SOC-002"]
        r1t["round 1: IN_REVIEW"] --> rr["READY<br/>(changes requested)"] --> r2t["round 2: IN_REVIEW"] --> am[AWAITING_MERGE]
    end
    subgraph round1["round 1 reviews"]
        a1["code_reviewer: FAIL"]
        b1["security_reviewer: PASS"]
    end
    subgraph round2["round 2 reviews"]
        a2["code_reviewer: PASS"]
        b2["security_reviewer: PASS"]
    end
    r1t -.- a1
    r1t -.- b1
    r2t -.- a2
    r2t -.- b2
```

Review events (REVIEW_CLAIMED, REVIEW_SUBMITTED, …) carry a `review_id` and leave the task's
`from_state`/`to_state` empty. Only the aggregator's task commands move the task, so the
event-log replay check from Phase 1 still holds.

### What changed in the task machine

| Command | Before Phase 3 | Now |
|---|---|---|
| `approve` | agent or human | **system (aggregator)** or human override |
| `request_changes` | agent or human | **system (aggregator)** or human override |
| `escalate` *(new)* | — | system: IN_REVIEW → WAITING_FOR_HUMAN when the round cap is hit or a review cannot complete |

The generated tests from chapter 9 picked this up automatically. Agent `approve` and agent
`request_changes` are now in the "wrong actor" cases, and `escalate` got its legal and
illegal cases without any test being written by hand.

## The aggregator: how a decision is made (T3)

Every time a review finishes, or the CI status changes, the **aggregator**
(`orchestrator/aggregator.py`) looks at the current round and decides. It is a pure function,
`decide()`, plus one call that applies the result. Its decision table:

```mermaid
flowchart TB
    start(["a review finished, or CI changed<br/>(task must be IN_REVIEW)"]) --> e{"any review<br/>ERROR?"}
    e -->|yes| esc1["escalate<br/>→ WAITING_FOR_HUMAN"]
    e -->|no| f{"any review FAIL,<br/>or CI failure for<br/>the head commit?"}
    f -->|yes| cap{"is this round 3?"}
    cap -->|yes| esc2["escalate<br/>→ WAITING_FOR_HUMAN"]
    cap -->|no| rc["request_changes<br/>→ READY, findings as feedback"]
    f -->|no| p{"all required reviews PASS<br/>and (CI not required or<br/>CI success for the head)?"}
    p -->|yes| ap["approve<br/>→ AWAITING_MERGE"]
    p -->|no| wait(["wait"])
```

Rules worth noticing:
- **One FAIL is enough.** The aggregator does not wait for the other reviewer, and the
  still-running review is superseded. This saves money.
- **CI is tied to a commit.** A green CI run for an *older* commit does not count. Every new
  submission resets the CI status.
- **A reviewer cannot wave through a serious problem.** A PASS that lists a `high` or
  `critical` finding is recorded as FAIL. The `policy_override` in the event says so.
- **Loops are bounded.** After three rounds the task goes to you, not round four.

`tests/unit/test_aggregator.py` checks the table as pure data: 13 situations, no database.

## Scenarios

### 1. Happy path: both reviewers pass, CI green

```mermaid
sequenceDiagram
    autonumber
    participant D as DEV-01 (developer)
    participant A as Axiom
    participant R as REV-01 (code review)
    participant S as SEC-01 (security review)
    participant G as GitHub sync (T6)
    D->>A: submit {commit abc123, PR #7}
    A->>A: TASK_SUBMITTED → IN_REVIEW<br/>REVIEW_REQUESTED ×2 (round 1)
    R->>A: POST /reviews/claim
    A-->>R: code review, task, diff info, developer's result
    S->>A: POST /reviews/claim
    A-->>S: security review
    R->>A: submit PASS
    A-->>R: decision: wait (security still running)
    S->>A: submit PASS
    A-->>S: decision: wait (CI unknown)
    G->>A: CI success for abc123
    A->>A: aggregator: approve → AWAITING_MERGE
```

If the project does not require CI, the second PASS approves immediately.

### 2. A reviewer finds a bug

```mermaid
sequenceDiagram
    autonumber
    participant R as REV-01
    participant A as Axiom
    participant S as SEC-01
    participant D as next developer run
    R->>A: submit FAIL [high] incidents.py:42 "groups alerts from different hosts"
    A->>A: aggregator: request_changes<br/>task → READY, review_rounds = 1
    A->>A: SEC-01's open review → SUPERSEDED
    Note over S: S's next heartbeat or submit is rejected<br/>(its review was superseded)
    D->>A: claim (same task, round 2)
    Note over D: prompt contains the findings:<br/>"[high] incidents.py:42 … → key by host too"
    D->>A: submit {commit def456}
    A->>A: REVIEW_REQUESTED ×2 (round 2, head def456)
```

### 3. CI fails

```mermaid
sequenceDiagram
    participant G as GitHub sync
    participant A as Axiom
    G->>A: CI failure for abc123 ("2 tests failed")
    A->>A: aggregator: request_changes (ci_failed = true)<br/>open reviews superseded
    Note over A: feedback for the developer includes<br/>"CI failed for abc123: 2 tests failed"
```

CI can send work back **before** the reviews finish, so reviewers don't spend money on a
change that doesn't even pass its tests.

### 4. Three rounds and still failing

```mermaid
flowchart LR
    r1["round 1<br/>FAIL"] -->|request_changes| d2["developer reworks"]
    d2 --> r2["round 2<br/>FAIL"] -->|request_changes| d3["developer reworks"]
    d3 --> r3["round 3<br/>FAIL"] -->|"escalate (cap = 3)"| you["WAITING_FOR_HUMAN<br/>you read the findings and<br/>answer, re-plan or cancel"]
```

### 5. A reviewer crashes or disappears

```mermaid
flowchart LR
    c["CLAIMED"] -->|"run failed (fail_run)<br/>or lease expired (reaper)"| p["PENDING again<br/>attempt 1, 2"]
    p -->|"claimed again"| c
    c -->|"3rd failure"| e["ERROR"] --> esc["aggregator: escalate<br/>→ WAITING_FOR_HUMAN"]
```

Failed review runs still cost money, and that cost is added to the task's spend.

### 6. A human overrides

You can still `approve` or `request_changes` a task in IN_REVIEW yourself (for example if a
reviewer is clearly wrong). Any open reviews are then superseded, and the event log shows
`human:<you>` as the actor.

## Reviewer work, like developer work

Reviewers use the same patterns as developers in Phase 1:

| | Developer | Reviewer |
|---|---|---|
| Claim | `POST /claims` | `POST /reviews/claim` |
| Lease | on the execution | on the review |
| Heartbeat | `POST /tasks/{key}/heartbeat` | `POST /reviews/{id}/heartbeat` |
| Finish | `submit` / `fail` / `ask_human` | `POST /reviews/{id}/submit` / `fail` |
| Reaper | times out executions | times out reviews too |
| Locks | task row → execution → agent | task row → review → agent |
| Who may | developers only (role = task's role) | reviewers only (role = review's role) |

A developer token gets 403 on `/reviews/claim`; reviewer agents can't be assigned developer
tasks (their role doesn't match). Cost: each review gets a run budget (`review_budget_usd`,
default $1, capped by the daily budget). Its cost is counted in the task's spend.

## What the event log looks like for a reviewed task

```text
TASK_SUBMITTED        IN_PROGRESS → IN_REVIEW   agent:DEV-01
REVIEW_REQUESTED      (code_reviewer, round 1)  system:reviews
REVIEW_REQUESTED      (security_reviewer, 1)    system:reviews
REVIEW_CLAIMED        code_reviewer             agent:REV-01
REVIEW_CLAIMED        security_reviewer         agent:SEC-01
REVIEW_SUBMITTED      code_reviewer: FAIL       agent:REV-01
TASK_REOPENED         IN_REVIEW → READY         system:aggregator   (findings in payload)
REVIEW_SUPERSEDED     security_reviewer         system:reviews
```

Each review event carries `review_id`, role, round, and the review's from/to state in its
payload. The task's own `from_state`/`to_state` columns are filled only for task transitions.

## Try it (so far)

1. `uv run pytest tests/unit/test_review_machine.py -v`: every review command × state, checked
   against the table in `docs/state-machines.md`.
2. `uv run pytest -k "wrong_actor and approve" -v`: an agent trying to approve is rejected.
3. `uv run pytest tests/unit/test_aggregator.py -v`: the decision table, one line per situation.
4. `uv run pytest tests/integration/test_reviews.py -v`: every scenario above against a real
   database, including two reviewers racing for one review.
