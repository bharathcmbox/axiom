# 6. Concurrency, leases and the reaper

Several workers run at once. They race to claim work, and sometimes crash
halfway through. This chapter explains the three mechanisms that keep that safe:

- **row locks**: two agents never act on the same task at the same time;
- **leases**: a crashed agent's task comes back;
- **capacity caps**: no more agents run than you are paying for.

## Row locks in 60 seconds

`SELECT … FOR UPDATE` locks the selected rows until the transaction ends. Any other
transaction that tries to lock the same row **waits**.

```mermaid
sequenceDiagram
    participant T1 as Transaction 1
    participant DB as tasks row AX-001
    participant T2 as Transaction 2
    T1->>DB: SELECT ... FOR UPDATE
    DB-->>T1: locked by T1
    T2->>DB: SELECT ... FOR UPDATE
    Note over T2: waits...
    T1->>DB: UPDATE state, COMMIT
    DB-->>T2: lock granted, sees T1's committed change
    T2->>T2: re-checks the state → command now illegal → rollback
```

This is why every command locks the task row first. Whoever gets there second sees the new
state and is rejected cleanly if its command no longer makes sense.

## Claiming work: `FOR UPDATE SKIP LOCKED`

When agents ask for "the next READY task", waiting is the wrong behaviour. Ten agents would
queue up behind the same row. `SKIP LOCKED` tells Postgres: *if the row is locked by someone
else, pretend it isn't there and give me the next one.*

```mermaid
sequenceDiagram
    participant D1 as DEV-01
    participant D2 as DEV-02
    participant DB as PostgreSQL
    Note over DB: READY: AX-001 (P1), AX-002 (P2)
    par both claim at the same instant
        D1->>DB: SELECT READY ... ORDER BY priority LIMIT 1 FOR UPDATE SKIP LOCKED
        DB-->>D1: AX-001 (now locked)
    and
        D2->>DB: SELECT READY ... ORDER BY priority LIMIT 1 FOR UPDATE SKIP LOCKED
        DB-->>D2: AX-001 is locked → skip → AX-002
    end
    D1->>DB: apply_locked(CLAIM) → AX-001 ASSIGNED, COMMIT
    D2->>DB: apply_locked(CLAIM) → AX-002 ASSIGNED, COMMIT
```

The test `test_ten_agents_racing_for_three_tasks_never_double_claim` releases 10 threads at
once (with a barrier) against 3 tasks. It checks that each task was claimed exactly once and
7 agents got nothing. It runs 3 times per test session.

## Leases: detecting dead workers

An agent can die at any moment: a crash, an OOM kill, a lost network. Axiom cannot "notice" a
death directly. Instead every active execution holds a **lease**, a deadline the agent must
keep pushing forward.

```mermaid
gantt
    title One execution's lease over time (lease = 300s)
    dateFormat  mm:ss
    axisFormat  %M:%S
    section Agent alive
    claim → lease until 05:00          :a1, 00:00, 5m
    heartbeat at 01:00 → until 06:00   :a2, 01:00, 5m
    heartbeat at 02:00 → until 07:00   :a3, 02:00, 5m
    section Agent dies at 02:30
    no more heartbeats                 :crit, 02:30, 4m30s
    section Reaper
    lease expired at 07:00 → time_out  :milestone, 07:00, 0m
```

```mermaid
flowchart LR
    hb["heartbeat(agent, task)"] --> lk["lock task row,<br/>then its active execution"]
    lk --> o{"agent owns it?"}
    o -->|no| e1[NotExecutionOwner]
    o -->|yes| l{"lease still live?"}
    l -->|no| e2["LeaseExpired<br/>(too late, it belongs to the reaper now)"]
    l -->|yes| ext["lease_expires_at = now() + lease_seconds<br/>no event written"]
```

Two details matter here:

- **The database clock is the only clock.** Every comparison is `lease_expires_at <= now()`
  inside Postgres. If the API server's clock drifted, nothing would break.
- **Heartbeats are not events.** They are not state changes, and logging one every minute per
  agent would drown the event log.

## The reaper

```mermaid
flowchart TB
    loop(["every reaper_interval_seconds (5s),<br/>and once at startup"]) --> q["SELECT tasks whose active execution<br/>has lease_expires_at <= now()"]
    q --> each{"for each candidate"}
    each --> tx["own transaction:<br/>SELECT task FOR UPDATE SKIP LOCKED"]
    tx -->|"row busy"| skip["skip, next sweep"]
    tx -->|locked| ap["apply_locked(TIME_OUT, system:reaper)"]
    ap -->|"guards re-check: still expired?"| ok["execution TIMED_OUT,<br/>task READY or FAILED,<br/>event EXECUTION_TIMED_OUT"]
    ap -->|"heartbeat or submit won the race"| skip2["LeaseNotExpired / IllegalTransition<br/>→ leave it alone"]
    ok --> each
    skip --> each
    skip2 --> each
```

The candidate query runs **without** locks, so by the time the reaper locks a row the agent
may have renewed the lease or finished. That's fine: `apply_locked` re-checks the guard
(`time_out` requires an expired lease) under the lock. **"Check, then act" is only safe
when the check is repeated under the lock.**

### The race between the reaper and a heartbeat

```mermaid
sequenceDiagram
    participant R as Reaper
    participant DB as PostgreSQL
    participant A as Agent heartbeat
    Note over DB: lease expires at 10:00:00.000
    R->>DB: candidates: AX-001 (lease <= now)
    A->>DB: lock AX-001 (FOR UPDATE)
    R->>DB: lock AX-001 SKIP LOCKED → busy → skip
    A->>DB: lease expired? yes → LeaseExpired (409)
    Note over R: next sweep, 5s later
    R->>DB: lock AX-001 → time_out → READY
```

Whichever side gets the lock first wins, and the other side sees a consistent state. A late
heartbeat cannot "resurrect" an expired lease, because the guard rejects it.

## Capacity caps

```mermaid
flowchart TB
    c(["claim / assign for agent X"]) --> la["lock agent X's row (FOR UPDATE)"]
    la --> pa{"X's active executions<br/>< X.max_concurrent?"}
    pa -->|no| r1["AssignmentRejected: at capacity"]
    pa -->|yes| dev{"X is a developer?"}
    dev -->|no| ok([proceed])
    dev -->|yes| adv["pg_advisory_xact_lock(DEVELOPER_CAP)"]
    adv --> g{"active developer executions<br/>< max_concurrent_developers (2)?"}
    g -->|no| r2["AssignmentRejected: global cap reached"]
    g -->|yes| ok
```

Why the locks? Without them, two developers could both count "1 active" at the same moment and
both proceed, giving 3 when the cap is 2. Locking the agent row serializes claims by the same
agent. The advisory lock (a named lock that isn't tied to any row) serializes all developer
claims, so the count-then-insert is effectively atomic. Test:
`test_global_developer_cap_holds_under_contention` (5 developers racing, exactly 2 win).

## Dependency promotion under concurrency

The subtle case: AX-003 depends on AX-001 and AX-002, and **both are merged at the same
instant** by two transactions.

```mermaid
sequenceDiagram
    participant T1 as merge AX-001
    participant DB as PostgreSQL
    participant T2 as merge AX-002
    T1->>DB: AX-001 → DONE (uncommitted)
    T2->>DB: AX-002 → DONE (uncommitted)
    T1->>DB: lock AX-003 FOR UPDATE
    T2->>DB: lock AX-003 FOR UPDATE → waits
    T1->>DB: deps DONE? AX-001 yes (mine), AX-002 not yet committed → no
    T1->>DB: COMMIT
    DB-->>T2: lock granted
    T2->>DB: deps DONE? AX-001 yes (now committed), AX-002 yes (mine) → promote
    T2->>DB: AX-003 → READY, COMMIT
```

Without the lock on AX-003, each transaction could see the other's merge as "not yet done",
and AX-003 would stay PENDING forever. The test runs this race 5 times
(`test_concurrent_merges_of_two_dependencies_promote_dependent`).

## Lock order: how deadlocks are avoided

A deadlock happens when T1 holds A and waits for B while T2 holds B and waits for A. Axiom
avoids it by always taking locks in the same order:

```mermaid
flowchart LR
    t["task row"] --> e["execution row"] --> a["agent row"] --> adv["advisory lock<br/>(developer cap)"]
```

## Try it

1. Watch a lease expire for real: `uv run pytest tests/integration/test_leases.py::test_lease_expires_on_the_real_clock -v`.
2. By hand: claim a task with `{"lease_seconds": 20}` on `POST /claims`, start it, then do
   nothing. Within about 25s, `GET /tasks/{key}/events` shows `EXECUTION_TIMED_OUT` by
   `system:reaper`.
3. Run the race tests a few times in a row; they should never flake:
   `for i in 1 2 3 4 5; do uv run pytest tests/integration/test_leases.py -q -k "racing or contention or concurrently"; done`
4. To see why the locks matter, comment out the `pg_advisory_xact_lock` line in
   `_resolve_assignee` and rerun `test_global_developer_cap_holds_under_contention` a few
   times. It should start failing intermittently. Then restore the line.
