# Phase 1 plan — control plane skeleton

Status: **approved 2026-11-02**. Source: the original ChatGPT design conversation, distilled into
[SPEC.md](../../SPEC.md). State machine diagrams: [docs/state-machines.md](../state-machines.md).

Scope: control plane only. No worker runtimes, no LLM calls, no GitHub integration.

## 1. Decisions

| # | Topic | Decision |
|---|---|---|
| D1 | First worker runtime | Claude Code (Claude Agent SDK) first, behind a runtime adapter; Codex later, mainly as an independent reviewer. |
| D2 | Source of truth for tasks | PostgreSQL. GitHub Issues/PRs mirror it (Phase 2+). |
| D3 | Engineering Manager | Deterministic code. LLMs only for judgment (spec decomposition, reports). |
| D4 | Axiom vs Agentic SOC | Separate. Axiom is product-agnostic; Agentic SOC is a customer project in its own repo. |
| D5 | GitHub integration | Phase 2. |
| D6 | Reviews table | Phase 3. In Phase 1 review outcomes are commands recorded in the event log. |
| D7 | Lease timeout | Counts as a failed attempt. Retry up to `max_retries` (default 2), then FAILED. |
| D8 | `requires_human_approval` | Dropped for now; every merge needs a human anyway. |
| D9 | Heartbeats | Extend the lease only; no event written. |
| D10 | Auth | None in Phase 1. API bound to 127.0.0.1; actor from `X-Axiom-Actor` header (`human:…`, `agent:DEV-01`, `system:reaper`). |
| D11 | Concurrency | At most 2 developer executions active at once, enforced at claim time. |
| D12 | Process | Plan → approval → one task at a time; lint + full test suite + one commit per task on `main`. |
| D13 | Stack | Python (3.14, via uv), FastAPI, psycopg 3 with raw SQL (no ORM), PostgreSQL 17, pytest against real Postgres, ruff, mypy, Docker Compose. |

## 2. State machines

Two machines: **task** (where the work is) and **execution** (one agent run on a task).
Diagrams and tables: [docs/state-machines.md](../state-machines.md).

Every command runs through one entry point, `apply_command`, in one transaction:
lock task row → check table + guards → update → append event(s). Illegal commands return 409
and change nothing. Execution status changes only as a side effect of task commands.

## 3. Schema

See `migrations/0001_init.sql`. Tables: `projects`, `agents` (registry), `tasks`,
`task_dependencies`, `executions` (with lease), `events` (append-only, enforced by trigger).

Cross-table invariants (e.g. a task is ASSIGNED/IN_PROGRESS iff it has exactly one active
execution) are checked by `check_invariants()` after every integration test and at startup.

## 4. Repository layout

```
axiom/
├── SPEC.md  CLAUDE.md  README.md  pyproject.toml  uv.lock  Makefile  .env.example
├── Dockerfile  docker-compose.yml
├── docs/
│   ├── plan/phase-1.md          # this file
│   ├── architecture.md  state-machines.md  api.md
│   └── decisions/ADR-00N-*.md
├── migrations/0001_init.sql
├── src/axiom/
│   ├── config.py
│   ├── state/        db.py  migrate.py  invariants.py
│   ├── domain/       task_machine.py  execution_machine.py  roles.py   # pure tables, no I/O
│   ├── orchestrator/ commands.py  assignment.py  leases.py  dependencies.py
│   ├── registry/     projects.py  agents.py
│   ├── events/       log.py
│   └── api/          app.py  schemas.py  errors.py  routes/
├── tests/unit/  tests/integration/
└── scripts/compose_smoke.sh  scripts/demo_restart.sh
```

Later-phase folders (`runtimes/`, `workspaces/`, `integrations/`, `agent_instructions/`,
`dashboard/`) are created when their phase starts.

## 5. Tasks

Every task ends with `make lint && make test` (results shown) and one commit.

| Task | Deliverable | Acceptance criteria | Proving tests |
|---|---|---|---|
| T0 Bootstrap | git, uv/pyproject, ruff, mypy, Makefile, compose `db`, SPEC.md, CLAUDE.md, ADR-001..005, this plan, state-machine diagrams | `make db` healthy; `make test` green | `test_smoke`: DB connection + `SELECT 1` |
| T1 Schema | `migrate.py`, `0001_init.sql` | Applies to empty DB; rerun is a no-op; constraints hold | rerun no-op; lease on ended execution rejected; second active execution per task rejected; UPDATE/DELETE on `events` raises |
| T2 State tables | `task_machine.py`, `execution_machine.py` | Match docs/state-machines.md exactly | every (command × state) pair checked; terminal states have no exits; cancel from every non-terminal; every state reachable |
| T3 Registry | projects + agents services, 7-agent seed config | Register/list/enable/disable agents; create projects; events written | CRUD; duplicate key rejected; invalid role/runtime rejected; event written |
| T4 Command engine + dependencies | `apply_command`, event log, non-execution commands | Atomic; one event per hop; illegal commands write nothing | one test per legal row, one per illegal pair (generated from table); forced event-insert failure rolls back; dependency promotion; cancelled dependency keeps task PENDING |
| T5 Assignment, executions, leases | claim/assign, start, submit, ask_human, fail, heartbeat, reaper, retry counting | Exclusive claims; role/concurrency/global caps; lease expiry → retry or FAILED | 10 threads × 3 tasks never double-claim; role mismatch rejected; third developer rejected; heartbeat extends; wrong agent rejected; timeout → READY, at limit → FAILED; reaper idempotent |
| T6 REST API | routes, schemas, error mapping, OpenAPI | Every command drivable over HTTP | PENDING→DONE over HTTP; illegal → 409 with allowed commands, state unchanged; malformed → 422; events endpoint matches path |
| T7 Compose | Dockerfile, `api` service, reaper in lifespan, `compose_smoke.sh` | Clean checkout → `docker compose up` → `/healthz` 200 | script: clone to temp → up → health → create task → down -v |
| T8 Restart durability | `demo_restart.sh` | Tasks in many states + one in-flight execution survive SIGKILL of api and db; snapshots identical; orphaned execution times out and retries | before/after diff of tasks, executions, events; pytest that rebuilds app + pool |
| T9 Docs + exit check | architecture.md, api.md, README install, known limitations, next phase | Every exit criterion mapped to evidence | summary table |

## 6. Phase 1 exit criteria

- [ ] Every legal transition has a test; every illegal transition is rejected with a test (T4, T5).
- [ ] Lease expiry returns a task to READY (or FAILED at the retry limit), with a test (T5).
- [ ] The event log records every transition (T4, T8).
- [ ] Killing and restarting the service preserves all state, demonstrated (T8).
- [ ] `docker compose up` brings up the API and Postgres from a clean checkout (T7).
