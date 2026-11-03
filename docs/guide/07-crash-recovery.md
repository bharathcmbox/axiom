# 7. Crash recovery

The goal, from the spec: *"Axiom must recover from a restart without relying on any agent's
conversation history."* Phase 1 meets it by keeping **nothing important in memory**.

## Where state lives

```mermaid
flowchart LR
    subgraph proc["API process (disposable)"]
        pool["connection pool"]
        th["reaper thread"]
        nothing["no caches, no queues,<br/>no in-memory task state"]
    end
    subgraph pg["PostgreSQL volume pgdata (durable)"]
        all["tasks · executions · leases ·<br/>events · agents · projects"]
    end
    proc <--> pg
```

Killing the process loses the pool and the thread, and both are rebuilt at startup.

Killing Postgres is safe as well. Postgres writes every commit to its write-ahead log on disk
before it acknowledges it. So anything Axiom was told "committed" survives a SIGKILL, and
anything not committed was never visible.

## What happens at startup

```mermaid
flowchart TB
    s(["container starts"]) --> m["python -m axiom.state.migrate<br/>(no-op if up to date)"]
    m --> sd["python -m axiom.registry.seed<br/>(adds only missing agents)"]
    sd --> app["create_app() lifespan"]
    app --> pool["open connection pool"]
    pool --> mig2["migrate() again (idempotent)"]
    mig2 --> inv["check_invariants() → log any violation"]
    inv --> sweep["reaper.sweep() once:<br/>time out leases that expired while we were down"]
    sweep --> thr["start reaper thread"]
    thr --> serve(["serve HTTP on :8000"])
```

The **startup sweep** is the recovery step. If a worker died while Axiom was down, its lease
has expired, and the task goes back to READY straight away. It does not wait for the first
5-second tick.

## What happens to work in flight

```mermaid
sequenceDiagram
    participant Dev as DEV-01 worker
    participant API as Axiom
    participant DB as PostgreSQL
    Dev->>API: start AX-009 (lease 30s)
    API->>DB: COMMIT (execution RUNNING)
    Note over API,DB: SIGKILL api + db
    Note over Dev: worker also dies, no more heartbeats
    Note over API,DB: restart (about 10s)
    API->>DB: startup sweep: lease not expired yet → nothing
    Note over DB: 30s lease runs out
    API->>DB: reaper: time_out AX-009 → READY (attempt_count 1)
    Note over DB: any developer can claim AX-009 again
```

If the worker had survived the outage, its next heartbeat would succeed (as long as the lease
had not expired), and it would carry on as if nothing happened.

## The demo, as a timeline

`make demo-restart` ([scripts/demo_restart.sh](../../scripts/demo_restart.sh)) proves all of
this against real containers:

```mermaid
flowchart LR
    a["create 9 tasks<br/>in 9 states<br/>(39 events)"] --> b["snapshot:<br/>tasks + executions<br/>+ events → JSON"]
    b --> c["docker compose kill<br/>-s SIGKILL api db<br/>(exit 137)"]
    c --> d["docker compose up<br/>--wait"]
    d --> e["snapshot again"]
    e --> f{"diff"}
    f -->|identical| g["wait for in-flight lease<br/>to expire"]
    g --> h["reaper: EXECUTION_TIMED_OUT,<br/>task READY"]
    h --> i["GET /invariants → []"]
```

The same guarantee is tested without Docker in `tests/integration/test_restart.py`. That test
builds the app, drives tasks, tears everything down, builds a new app, and compares.

## Try it

1. `make demo-restart` and read the output top to bottom.
2. While a task is IN_PROGRESS, run `docker compose restart api`. Then call `heartbeat` as the
   owning agent: it still works, because the lease lives in the database, not in the process.
3. `docker compose down` (without `-v`) and then `up` again: all state is still there.
   `down -v` deletes the volume and therefore everything.
