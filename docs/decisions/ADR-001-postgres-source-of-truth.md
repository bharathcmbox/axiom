# ADR-001: PostgreSQL is the source of truth for task state

Status: Accepted (2026-11-02)

## Context
The original design listed GitHub Issues as "backlog and task state" and also a Postgres tasks
table. Two writable sources of truth would drift, and GitHub cannot give us atomic transitions,
row locks for exclusive claims, or an append-only event log in the same transaction.

## Decision
All task, execution and event state lives in PostgreSQL. GitHub Issues and PRs mirror it
(Phase 2+). Changes made in GitHub reach Axiom only as explicit, validated commands.

## Consequences
- Transitions, leases and events are atomic and recoverable after a restart.
- Need a sync component later; GitHub can briefly lag Axiom.
