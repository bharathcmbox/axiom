# Architecture

The control plane first (Phase 1), then workers and sandboxes (Phase 2), then reviews, the
aggregator and the GitHub sync (Phase 3), then planning, QA and hardening (Phase 4), at the end of this page. To learn it, read
[the guide](guide/README.md).

Axiom's control plane is a single FastAPI process in front of PostgreSQL. All state lives in
Postgres; the process keeps nothing in memory that matters, so it can be killed at any time.
Decisions behind this design: [docs/decisions/](decisions/).

```
            HTTP (Authorization: Bearer <token> → human:… | agent:DEV-01)
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

## Workers and sandboxes (Phase 2, ADR-008)

```
worker (deterministic harness; holds GitHub + agent tokens)
  claim → start → heartbeat thread → fresh clone (no credentials in it)
    → sandbox container (LLM; no credentials, a run token for the model proxy; egress via allowlist proxy; read-only root)
    → verify diff (paths, secrets, symlinks) → commit → tests in a fresh sandbox, clean clone
    → push axiom/* (never forced) → open or reuse PR → submit
```

Workers use only the public API. Budgets are enforced at claim time and per run. No code path can
merge. Detail: [guide chapter 11](guide/11-workers-and-sandbox.md).

## Reviews, the aggregator and the GitHub sync (Phase 3, ADR-009, ADR-010)

```
submit → one review per required role (reviews table: own state machine, leases)
reviewer worker → workspace at the submitted commit → read-only sandbox (code: Fable 5.2, security: Sonnet 5.5) → review contract
github-sync (read-only) → CI result for the head commit, merges, closes, change requests
aggregator (pure decide()) → approve | request_changes | escalate | wait
human merges on GitHub → sync → merge → DONE, recorded as human:github-<login>
```

`approve` and `request_changes` are system commands, with a human override. Agents cannot issue
them. Lock order everywhere: task row → execution or review row → agent row → advisory lock.
Detail: [guide chapter 12](guide/12-reviews-and-ci.md); every scenario:
[chapter 13](guide/13-scenarios.md).

## Planning, QA, parallel work and metrics (Phase 4, ADR-011 to ADR-017)

```
human → spec + budget → plans table (own state machine, leases)
planner worker (EM-01, Opus 5.5) → read-only sandbox → plan contract
plan_rules (pure) → broken: back to the planner | ok: PROPOSED → human approves → every task created, or none
tasks → up to two developers at once on independent tasks; QA tasks wait for the work they test
sync sees mergeable=false → report_conflict → the agent merges main and resolves the markers on the same PR
metrics (pure, from the event log) → per task, per plan (spec to merge, time saved by parallel work) → /metrics, report, /ui
```

Hardening: one service holds each secret or capability. `model-proxy` holds the Anthropic key and meters every
run (ADR-015), `sandbox-launcher` holds the Docker socket (ADR-016), and the harness uses a sealed copy of `.git`
(ADR-014). Workers sit on internal networks and reach only GitHub (ADR-017). Budgets are reserved at claim time;
a review gets its share of what the task has left (ADR-013). Reviewer models are set per agent, and the registry
enforces that they differ from the developers' (ADR-009). Detail: [guide chapter 16](guide/16-planning.md),
[chapter 15](guide/15-metrics.md), [chapter 11](guide/11-workers-and-sandbox.md).
