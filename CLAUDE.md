# Axiom — an AI engineering organization (control plane)

Axiom coordinates coding agents (Claude Code first, Codex later) through a backlog, state machines,
leases, and an event log. See SPEC.md and docs/plan/phase-1.md. Current phase: **1 — control plane
skeleton** (no runtimes, no LLM calls, no GitHub integration yet). Axiom is product-agnostic: no
SOC/security-product concepts in this repo; customer projects live in their own repos.

## Commands
- `make db` — start Postgres only (needed for tests)
- `make up` — docker compose up: Postgres + API on 127.0.0.1:8000 (OpenAPI at /docs)
- `make test` — full pytest suite against real Postgres (`axiom_test`)
- `make lint` — ruff check, ruff format --check, mypy
- `make fmt` — ruff format + ruff check --fix
- `make migrate` — apply migrations/*.sql (also runs on API startup)
- `make demo-restart` — kill/restart durability demo
- Use `uv run …`; never pip install globally.

## Architecture rules
- `domain/*_machine.py` are pure transition tables, the ONLY place legality is defined.
  docs/state-machines.md must match them (a unit test checks this).
- `orchestrator.commands.apply_command` is the ONLY code that changes task or execution state:
  one transaction = lock task row → check table + guards → update → append event(s).
- Execution status changes only as a side effect of task commands.
- Leases live on executions; all time math uses the DB clock (`now()`). The reaper issues
  `time_out` for expired leases; it runs in the API process and once at startup.
- `events` is append-only (DB trigger). Heartbeats are not events.
- Postgres is the source of truth. GitHub (Phase 2+) mirrors it.
- Raw SQL with psycopg 3, no ORM. Schema change = new numbered migration; never edit an applied one.

## Conventions
- Every transition or guard needs a legal-path test AND an illegal-path test asserting
  state unchanged and no event written.
- Integration tests use real Postgres (no DB mocks) and call `check_invariants()` on teardown.
- Domain errors are typed exceptions; only `api/errors.py` maps them to HTTP status codes.
- Significant design decisions get an ADR in docs/decisions/.
- After each task: `make lint && make test`, then one commit per task.
