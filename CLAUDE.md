# Axiom — an AI engineering organization (control plane)

Axiom coordinates coding agents (Claude Code first, Codex later) through a backlog, state machines,
leases, and an event log. See SPEC.md, docs/plan/ and docs/guide/. Phases 1 (control plane),
2 (first worker) and 3 (independent review, CI gate, GitHub sync) are done; next is Phase 4
(engineering management). Axiom is product-agnostic: no SOC/security-product concepts in this repo;
customer projects live in their own repos.

## Commands
- `make db` — start Postgres only (needed for tests)
- `make up` — docker compose up: Postgres + API on 127.0.0.1:8000 (OpenAPI at /docs)
- `make test` — full pytest suite against real Postgres (`axiom_test`)
- `make lint` — ruff check, ruff format --check, mypy
- `make fmt` — ruff format + ruff check --fix
- `make migrate` — apply migrations/*.sql (also runs on API startup)
- `make demo-restart` — kill/restart durability demo
- `make worker-tokens` / `make workers` / `make workers-down` — developers, reviewers, GitHub sync
- `make test-docker` — sandbox isolation tests; `make test-live` — real API (announce first, costs money)
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
- Postgres is the source of truth. GitHub mirrors it (PRs now; Issues later).
- Auth: every route but /healthz needs a bearer token; the actor comes from the token.
  `system:` actors never authenticate over HTTP.
- Workers (ADR-008): `worker/` is deterministic harness code holding the GitHub and agent
  tokens; the LLM runs only in `sandbox/` containers with just the Anthropic key, no settings
  from the repo (`setting_sources=[]`), egress via the allowlist proxy. Never pass other
  credentials into a sandbox. Pushes only to `axiom/*`; no merge code path (ADR-007).
- Budgets are enforced at claim time and per run (`orchestrator/budgets.py`); reviews count too.
- Reviews (ADR-009): `domain/review_machine.py` holds their legality; review events never move the
  task. Only `orchestrator/aggregator.py` turns reviews + CI into approve / request_changes /
  escalate. No agent can approve. Reviewers run read-only on a different model.
- GitHub sync (ADR-010): read-only towards GitHub; humans merge there; a merge is recorded as
  `human:github-<login>`.
- Raw SQL with psycopg 3, no ORM. Schema change = new numbered migration; never edit an applied one.

## Conventions
- Every transition or guard needs a legal-path test AND an illegal-path test asserting
  state unchanged and no event written.
- Integration tests use real Postgres (no DB mocks) and call `check_invariants()` on teardown.
- Domain errors are typed exceptions; only `api/errors.py` maps them to HTTP status codes.
- Significant design decisions get an ADR in docs/decisions/.
- After each task: `make lint && make test`, then one commit per task. When chaining commands
  through a pipe, use `set -o pipefail` so a failure isn't hidden.
- Docs are part of every task: docs/guide/ with diagrams (validated by the Mermaid parser) and
  chapter 13 for any new scenario. Tests named in the guide must exist (a unit test checks).
