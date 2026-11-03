# Architecture (Phase 1)

Axiom's control plane is a single FastAPI process in front of PostgreSQL. All state lives in
Postgres; the process keeps nothing in memory that matters, so it can be killed at any time.
Decisions behind this design: [docs/decisions/](decisions/).

```
            HTTP (X-Axiom-Actor: human:… | agent:DEV-01 | system:…)
                │
┌───────────────▼────────────────────────────────────────────────┐
│ api/            routes.py (thin) · schemas.py · errors.py       │
├────────────────────────────────────────────────────────────────┤
│ orchestrator/   commands.py   apply_command / apply_locked      │ ◄── the ONLY writer of
│                 assignment.py claim_next (SKIP LOCKED)          │     task/execution state
│                 leases.py     heartbeat · expire_leases         │
│                 reaper.py     background sweep thread           │
│                 tasks.py      create_task · reads               │
│                 dependencies.py                                  │
├────────────────────────────────────────────────────────────────┤
│ registry/       projects.py · agents.py · seed.py               │
│ events/         log.py        append_event (only writer)        │
│ domain/         task_machine.py · execution_machine.py · roles  │ ◄── pure tables, no I/O
├────────────────────────────────────────────────────────────────┤
│ state/          db.py · migrate.py · invariants.py              │
└───────────────▲────────────────────────────────────────────────┘
                │ psycopg 3, raw SQL, connection pool
          PostgreSQL 17: projects · agents · tasks · task_dependencies · executions · events
```

## The command pattern

Every state change is a **command** on a task, applied in one transaction:

1. `SELECT … FROM tasks WHERE key = … FOR UPDATE` locks the task.
2. `task_machine.check(command, state, actor.kind)` raises `IllegalTransition` or
   `ActorNotPermitted` before anything is written.
3. Guards: dependencies DONE (`promote`); agent enabled, role match, per-agent and global
   capacity (`claim`/`assign`); execution owner and live lease (agent commands); expired lease
   (`time_out`).
4. Side effects on the execution (create, start, complete, fail, time out, cancel), driven by
   `execution_machine`.
5. Update the task (state, counters), then `append_event`.
6. On `merge`, promote dependents whose dependencies are now all DONE.

If any step fails, the transaction rolls back: no state change, no execution change, no event.
A test forces the event insert to fail to prove it.

## Concurrency

| Situation | Mechanism |
|---|---|
| Two agents claim at once | `FOR UPDATE SKIP LOCKED` on the READY row: each claimer gets a different task or none |
| Same agent claims twice | Agent row `FOR UPDATE`, then count its active executions |
| Global developer cap (2) | `pg_advisory_xact_lock` around the count of active developer executions |
| Two dependencies merged at once | Each merge locks the dependent before reading dependency states; under READ COMMITTED the second sees both DONE |
| Reaper vs heartbeat vs agent command | All lock the task row first, then the execution; guards are re-checked under the lock |

**Lock order everywhere:** task row → execution row → agent row → advisory lock. Following one
order avoids deadlocks.

## Leases and recovery

- `claim`/`assign` create an execution with `lease_expires_at = now() + lease_seconds`.
  `start` and heartbeats renew it. Lease time always comes from the **database clock**.
- The reaper (`expire_leases`) runs every `AXIOM_REAPER_INTERVAL_SECONDS` and **once at
  startup**, so executions orphaned while Axiom was down are recovered immediately.
- A timeout counts as a failed attempt. The task returns to READY while
  `attempt_count ≤ max_retries`, otherwise it goes to FAILED.
- Restart safety is proven by `tests/integration/test_restart.py` and
  `scripts/demo_restart.sh` (SIGKILL of both API and Postgres).

## Invariants

Postgres enforces what it can:
- an execution has a lease iff it is active, and has `ended_at` iff it is terminal;
- at most one active execution per task;
- `events` is append-only (trigger);
- CHECK constraints on enums.

`state/invariants.py` checks the cross-table rules:
- a task is executing iff it has exactly one active execution;
- each task's events replay into a chain that ends at its current state;
- dependency consistency;
- retry counters;
- capacity limits.

These run after every integration test and at startup.

## Extending

- **New command or state:** edit `domain/task_machine.py` and `docs/state-machines.md`
  together (a unit test keeps them in sync), then handle side effects in `apply_locked`. The
  generated legal, illegal and wrong-actor tests pick it up automatically.
- **Schema change:** add `migrations/000N_*.sql`. Never edit an applied migration.
- **Phase 2 workers** talk to the existing API: `POST /claims` → `start` → heartbeats →
  `submit`, `ask_human` or `fail`.
