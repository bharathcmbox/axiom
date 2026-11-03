# ADR-005: Raw SQL with psycopg 3 and plain SQL migrations; no ORM

Status: Accepted (2026-11-02)

## Context
The core of Phase 1 is row locking (`FOR UPDATE`, `SKIP LOCKED`), constraints and
transactional event writes. An ORM hides exactly the parts that matter for this project.

## Decision
Use psycopg 3 with explicit SQL and a connection pool. Schema changes are numbered `.sql` files in
`migrations/`, applied by a small runner (`python -m axiom.state.migrate`) that records them in
`schema_migrations` and holds an advisory lock. Applied migrations are never edited.

## Consequences
- Locking and transaction boundaries are visible in the code.
- More hand-written SQL and row mapping; acceptable at this size.
