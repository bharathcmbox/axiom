# 10. Code tour and how to extend it

About 2,000 lines of Python in `src/axiom/`. This chapter maps them and walks through a real
change.

## Module dependencies

The arrows point from a module to what it imports. The layering is strict: `domain` imports
nothing from Axiom, and nothing imports `api` except the app.

```mermaid
flowchart TB
    subgraph L4["api: HTTP"]
        app[api/app.py]
        routes[api/routes.py]
        errs[api/errors.py]
        sch[api/schemas.py]
    end
    subgraph L3["orchestrator: behaviour"]
        cmd[commands.py]
        asg[assignment.py]
        lea[leases.py]
        rea[reaper.py]
        tsk[tasks.py]
        dep[dependencies.py]
    end
    subgraph L2["registry + events"]
        agt[registry/agents.py]
        prj[registry/projects.py]
        log[events/log.py]
    end
    subgraph L1["domain: pure rules"]
        tm[task_machine.py]
        em[execution_machine.py]
        rol[roles.py]
    end
    subgraph L0["state: database plumbing"]
        db[state/db.py]
        mig[state/migrate.py]
        inv[state/invariants.py]
        cfg[config.py]
    end
    app --> routes
    app --> errs
    app --> rea
    app --> mig
    app --> inv
    routes --> sch
    routes --> cmd
    routes --> asg
    routes --> lea
    routes --> tsk
    routes --> agt
    routes --> prj
    asg --> cmd
    lea --> cmd
    rea --> lea
    tsk --> cmd
    cmd --> tm
    cmd --> em
    cmd --> dep
    cmd --> log
    cmd --> agt
    tsk --> log
    agt --> log
    prj --> log
    log --> db
    em --> tm
    tm --> rol
```

## File by file

| File | Lines (approx.) | Read it for |
|---|---|---|
| `domain/task_machine.py` | 190 | The rules. Start here. |
| `domain/execution_machine.py` | 50 | How executions follow tasks |
| `orchestrator/commands.py` | 280 | **The engine**: `apply_locked`, guards, side effects, capacity |
| `orchestrator/assignment.py` | 40 | `SKIP LOCKED` claiming |
| `orchestrator/leases.py` | 100 | Heartbeats and the reaper sweep |
| `orchestrator/tasks.py` | 190 | Task creation, keys, dependency validation |
| `state/invariants.py` | 115 | What "healthy" means |
| `migrations/0001_init.sql` | 110 | The schema and its constraints |
| `api/routes.py` | 230 | Endpoints (thin) |
| `api/app.py` | 70 | Startup and shutdown |

## Configuration

```mermaid
flowchart LR
    env["environment variables<br/>AXIOM_* (or .env)"] --> s["Settings (pydantic-settings)<br/>config.py, cached"]
    s --> u1["database_url"]
    s --> u2["lease_seconds = 300"]
    s --> u3["reaper_interval_seconds = 5"]
    s --> u4["max_concurrent_developers = 2"]
    s --> u5["migrations_dir"]
```

In docker compose these are set in `docker-compose.yml`. For tests, `test_database_url`
points at `axiom_test`.

## Worked example: adding a new command

Suppose Phase 3 needs a command `block` that a human uses to park a READY task (state
BLOCKED) and `unblock` to release it. These are the steps, in order:

```mermaid
flowchart TB
    s1["1. migrations/0002_blocked.sql<br/>ALTER the tasks.state CHECK to allow BLOCKED"] --> s2
    s2["2. domain/task_machine.py<br/>add TaskState.BLOCKED,<br/>TaskCommand.BLOCK / UNBLOCK,<br/>two Rule(...) rows"] --> s3
    s3["3. docs/state-machines.md<br/>add rows to the table + diagram<br/>(the doc-sync test forces this)"] --> s4
    s4["4. tests: helpers.py DEFAULT_ACTOR + PATH entry for BLOCKED,<br/>doc_tables.py TASK_STATES list"] --> s5
    s5["5. make test<br/>the generated legal/illegal/actor tests<br/>now cover block + unblock automatically"] --> s6
    s6["6. API: nothing to do.<br/>PUBLIC_COMMANDS is derived from TaskCommand"]
```

You did not need to touch `apply_locked`, because these commands have no side effects beyond
the state change. Note the cancel rule: `NON_TERMINAL_STATES` is computed, so BLOCKED is
automatically cancellable.

## Where Phase 2 plugs in

```mermaid
flowchart LR
    subgraph new["Phase 2 (new)"]
        w["worker process<br/>(one per concurrent developer)"]
        ad["runtimes/claude_code.py<br/>Claude Agent SDK adapter"]
        ws["workspaces/<br/>git worktree + container"]
        gh["integrations/github.py<br/>branch, PR, Issues mirror"]
    end
    subgraph existing["Phase 1 (unchanged API)"]
        api["POST /claims<br/>commands/start<br/>heartbeat<br/>commands/submit | fail | ask_human"]
    end
    w -->|HTTP| api
    w --> ad
    w --> ws
    w --> gh
```

The worker is just another API client, doing exactly what you did by hand with curl. The
control plane doesn't need to change for Phase 2 to work, apart from adding authentication and
recording cost.

## Exercises

1. **Read path:** starting from `POST /claims` in `routes.py`, follow the call chain to the
   `INSERT INTO executions`. Write down every lock taken, in order.
2. **New invariant:** add a check to `invariants.py` that every DONE task has at least one
   COMPLETED execution. Add a corruption test for it in `test_invariants.py`.
3. **New endpoint:** add `GET /agents/{key}/executions` listing an agent's executions. It is a
   read-only route, so it does not touch the engine.
4. **Thought experiment:** what would break if `expire_leases` locked with plain `FOR UPDATE`
   instead of `SKIP LOCKED`? (Hint: who would it wait for, and what happens to the other
   expired tasks meanwhile?)
