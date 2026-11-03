# Axiom

*Intelligence, orchestrated.* An AI engineering organization: a control plane that runs a team of
coding agents on real software work. See [SPEC.md](SPEC.md).

Status: Phase 1 (control plane skeleton) in progress. Plan: [docs/plan/phase-1.md](docs/plan/phase-1.md).

## Development

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
make db      # start Postgres
make test    # run the test suite
make lint    # ruff + mypy
```
