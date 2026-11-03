# Phase 1 plan — control plane skeleton

Status: **approved 2026-11-02**. Revised during T4: execution side effects moved from T5 into T4
(needed to reach every state through real commands); T5 keeps caps, heartbeats and the reaper. Source: the original ChatGPT design conversation, distilled into
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
| T4 Command engine + dependencies | `apply_command`, event log, all 15 commands incl. execution side effects, owner and lease-live guards, basic `claim_next`, `check_invariants` | Atomic; one event per hop; illegal commands write nothing | one test per legal row, one per illegal pair, one per wrong actor kind (generated from table); forced event-insert failure rolls back; dependency promotion incl. concurrent merges; cancelled dependency keeps task PENDING; invariant checker detects corruption |
| T5 Concurrency, leases, reaper | per-agent and global developer caps, heartbeat, reaper loop | Exclusive claims under contention; caps hold under contention; lease expiry → retry or FAILED | 10 threads × 3 tasks never double-claim; third developer rejected; heartbeat extends; heartbeat by wrong agent / after expiry rejected; reaper times out only expired leases; reaper idempotent |
| T6 REST API | routes, schemas, error mapping, OpenAPI | Every command drivable over HTTP | PENDING→DONE over HTTP; illegal → 409 with allowed commands, state unchanged; malformed → 422; events endpoint matches path |
| T7 Compose | Dockerfile, `api` service, reaper in lifespan, `compose_smoke.sh` | Clean checkout → `docker compose up` → `/healthz` 200 | script: clone to temp → up → health → create task → down -v |
| T8 Restart durability | `demo_restart.sh` | Tasks in many states + one in-flight execution survive SIGKILL of api and db; snapshots identical; orphaned execution times out and retries | before/after diff of tasks, executions, events; pytest that rebuilds app + pool |
| T9 Docs + exit check | architecture.md, api.md, README install, known limitations, next phase | Every exit criterion mapped to evidence | summary table |

## 6. Phase 1 exit criteria: met (2026-11-02)

| Criterion | Evidence |
|---|---|
| Every legal transition has a test | `tests/integration/test_transitions.py::test_legal_transition`: 27 cases generated from the table (every command × source state; fail/time_out in both the retry and exhausted branches), each asserting target state, exactly one event and the execution side effect. `promote` is covered by the dependency tests in `test_tasks.py`. `test_legal_cases_cover_every_rule` fails if a rule is left untested. |
| Every illegal transition is rejected, with a test | `test_illegal_transition_rejected`: 108 (command, state) pairs, each asserting `IllegalTransition`, state unchanged, no execution change and no event. Also: 24 wrong-actor-kind cases, 9 "claim ignores non-READY" cases, guard tests (owner, lease, role, capacity, dependencies), 150 + 90 pure-table pairs in `tests/unit/`, and HTTP 409 in `test_api.py`. |
| Lease expiry returns a task to READY, with a test | `test_leases.py`: `test_reaper_times_out_only_expired_leases`, `test_lease_expires_on_the_real_clock` (real 1s lease), `test_reaper_fails_task_once_retries_are_used_up`, `test_background_reaper_thread`; `test_restart.py::test_startup_times_out_executions_orphaned_while_down`. |
| The event log records every transition | Every command appends its event in the same transaction (`test_event_write_failure_rolls_back_the_transition`). `check_invariants` replays each task's events into a chain ending at its current state; it runs after every integration test, and `test_invariants.py` proves it catches a state change made without an event. |
| Killing and restarting preserves all state | `test_restart.py::test_restart_preserves_all_state`, plus `scripts/demo_restart.sh`: 9 tasks in 9 states and 39 events; SIGKILL of api and db (exit 137); full before/after snapshot diff identical; the orphaned in-flight task is then timed out by the reaper. |
| `docker compose up` from a clean checkout | `scripts/compose_smoke.sh` clones HEAD into a temp dir and runs `docker compose up --build --wait` under its own project and ports: both containers healthy, 7 agents seeded, task created and claimed, invariants empty. |

Final run: 634 tests passing; ruff and mypy (strict) clean.

## 7. Known limitations

- **No authentication.** The `X-Axiom-Actor` header is trusted, so anyone who can reach the API
  can act as `human:` or `system:`. Mitigation for now: bound to 127.0.0.1. Must be fixed
  before workers run unattended (Phase 2: per-agent tokens).
- **Dependencies are immutable, and a CANCELLED dependency leaves its dependents PENDING** until
  a human cancels them. There is no re-planning or task editing yet.
- **`budget_usd`, `branch`, `pull_request_url`** and execution cost/token columns are stored
  but not used until Phase 2.
- **Reviews are commands only.** Findings live in event payloads; there is no `reviews` table or
  review contract validation until Phase 3. Role checks such as "a reviewer, not the author,
  approves" are not enforced yet (only actor kind is).
- **The global developer cap uses one advisory lock,** which serializes developer assignments.
  That is fine at 2 developers; revisit if the cap grows.
- **`check_invariants` scans all events.** Cheap now; at startup it will need bounding as the
  log grows.
- **Event `occurred_at` is per transaction** (`now()`). Order events by `id`.
- **No pagination on `/tasks`.** `/events` pages via `after_id`.
- **The default lease (300s) suits driving tasks by hand.** Workers should use shorter leases
  plus heartbeats.
- Running several API instances should be safe (all coordination is in Postgres locks) but
  has not been tested.

## 8. Recommended next phase: Phase 2, the first worker

1. **Worker loop** (a separate process, not the API): `POST /claims` → `start` → heartbeat
   thread → run → `submit`, `ask_human` or `fail`. Uses only the existing API.
2. **Claude Code runtime adapter** (Claude Agent SDK) behind `runtimes/base.py`
   (`start`, stream events, `cancel`, collect result) per ADR-003.
3. **Workspace isolation:** a git worktree plus container per execution; scoped GitHub token.
4. **Result contract:** a JSON schema validated on `submit`; cost and tokens written to
   `executions`; budget cap enforced (stop the run, then `fail`).
5. **GitHub integration:** branch and PR creation; mirror tasks to Issues (ADR-001).
6. **Auth:** per-agent API tokens replace the trusted header for agents.

Decisions needed before Phase 2:
- the first target repository (something small, not Agentic SOC);
- API-key billing for unattended runs;
- the model choice for developers.
