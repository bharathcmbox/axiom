# ADR-011: Plans are first-class; only a human's approval turns one into work

Status: Accepted (2026-11-06)

## Context
Phase 4 adds a planner agent that turns a spec into tasks. SPEC: "I approve it before anything runs."
A model's plan is untrusted output and could be cyclic, overlapping, too expensive, or
over-privileged.

## Decision
- A `plans` table with its own state machine, in the style of reviews (lease-based claims, retries,
  ERROR after 3 attempts).
- The planner runs read-only in the sandbox and returns a strict **plan contract**.
- Deterministic validation runs **before a human sees the plan**: no cycles, safe `allowed_paths`, QA
  tasks limited to test paths, tasks that can run at the same time don't overlap, budgets within the
  plan's limit, at most 8 tasks. A failure counts as a failed attempt.
- Approval creates all tasks in **one transaction**, with the approving human as actor, so "only humans
  create tasks" still holds. Rejection sends feedback to the next attempt.
- QA tasks are ordinary tasks with role `qa`; no separate stage.

## Consequences
- A plan can propose work but grants nothing: tasks run under all existing limits.
- The human reads every plan. That is a bottleneck by design, and the metrics will show its cost.
