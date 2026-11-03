# ADR-009: Reviews are first-class work; deterministic aggregation decides

Status: Accepted (2026-11-03)

## Context
Phase 3 adds independent reviewers. A review could be modelled as another execution of the
task, or as separate work. The decision to send work back or forward could be made by a model
(an "engineering manager" agent) or by code.

## Decision
- A `reviews` table with its own state machine (PENDING → CLAIMED → PASS / FAIL / ERROR, or
  SUPERSEDED). One review per required role (code_reviewer, security_reviewer) is created
  when a task is submitted. Reviewers claim reviews with leases, like developers claim tasks.
- Reviews return a strict **review contract** (verdict, summary, findings with severity,
  file, line, issue, required_change).
- A deterministic **aggregator** issues `approve` (all reviews PASS and CI success) or
  `request_changes` (any FAIL, or CI failure). These become **system** commands; agents can
  no longer issue them. A human can still override.
- Policy: a PASS that contains a `high` or `critical` finding is treated as FAIL. After 3
  review rounds the task goes to WAITING_FOR_HUMAN instead of looping.
- Reviewers run on a different model from developers (Claude Fable 5.1 vs Claude Opus 5.5),
  read-only (Read/Glob/Grep; every edit denied; no Bash).

## Consequences
- "Nobody approves their own work" is structural: developers cannot claim reviews, and no
  agent can approve.
- The Phase 1 invariant (executing ⇔ one active execution) is unaffected.
- Review cost is visible per review and per task.
