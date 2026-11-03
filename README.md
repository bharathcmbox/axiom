# Axiom

*Intelligence, orchestrated.* An AI engineering organization: a control plane that runs a team of
coding agents (Claude Code first, Codex later) on real software work, with you as CTO.
See [SPEC.md](SPEC.md).

**Status: Phase 1 (control plane skeleton) complete.** Projects, an agent registry, task and
execution state machines, leases with heartbeats and a reaper, an append-only event log, and
a REST API. No worker runtimes or LLM calls yet.

## Quick start

Requires Docker. For development you also need [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --build --wait    # Postgres + API on http://127.0.0.1:8000
open http://127.0.0.1:8000/docs         # interactive API docs
```

On startup the API applies migrations and registers the seven agents in
[config/agents.toml](config/agents.toml). To drive a task through its life by hand, follow
the walkthrough in [docs/api.md](docs/api.md#walkthrough-one-task-from-creation-to-done-by-hand).

## Development

```bash
make db             # start Postgres only
make test           # full test suite against real Postgres (axiom_test)
make lint           # ruff + mypy (strict)
make up / down      # whole stack
make smoke          # prove `docker compose up` works from a clean clone
make demo-restart   # SIGKILL api + db mid-flight, restart, show state is intact
```

Configuration is through environment variables (`AXIOM_*`, see `src/axiom/config.py` and
`.env.example`): `DATABASE_URL`, `LEASE_SECONDS` (300), `REAPER_INTERVAL_SECONDS` (5),
`MAX_CONCURRENT_DEVELOPERS` (2).

## Documentation

- **[docs/guide/](docs/guide/README.md): start here to learn how Axiom is built and how it works (10 chapters, about 60 diagrams)**
- [SPEC.md](SPEC.md): what Axiom is and the phases
- [docs/state-machines.md](docs/state-machines.md): task and execution state machines (diagrams + tables)
- [docs/architecture.md](docs/architecture.md): components, command pattern, concurrency, recovery
- [docs/api.md](docs/api.md): endpoints, errors, curl walkthrough
- [docs/decisions/](docs/decisions/): architecture decision records
- [docs/plan/phase-1.md](docs/plan/phase-1.md): Phase 1 plan, exit evidence, limitations, next phase
