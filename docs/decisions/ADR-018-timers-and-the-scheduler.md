# ADR-018: Timers, and a scheduler that only creates work

Status: Accepted (2026-11-13)

## Context
Axiom only acts when a human creates a plan or a task. Phase 5 runs it unattended for days, so it has to wake up
by itself, both for recurring work ("every Monday, propose a dependency update") and to follow up on work that
has stalled ("a PR has waited 2 days"). Anthropic's "Building always-on agents" (2026-11-13) describes the
pattern: timers in a durable store of their own, and a scheduler that polls for due timers and turns them into
events. Axiom already has the rest of that pattern: a durable backlog, claims, leases and a reaper.

## Decision
- **A `timers` table** with a pure transition table, `domain/timer_machine.py`: `fire` keeps a recurring timer
  PENDING with its next due time and makes a one-off FIRED; `cancel` makes a pending timer CANCELLED. docs/state-machines.md matches it, like the other machines.
- **Two kinds.** *Schedules* are recurring and created only by a human, from a plan template the human wrote.
  *Follow-ups* are one-off and created only by deterministic code, in the same transaction as the event that
  needs one (for example `ask_human` sets "remind in a day"), and cancelled when their subject moves on.
  **Agents cannot create, change or cancel timers.**
- **The scheduler is the reaper's sweep,** in the API process, on the DB clock (`now()`), and once at startup. Each due timer is
  fired in one transaction: lock the timer row, check its state, create the work through the plan service or
  `apply_command`, advance or close the timer, append an event. The lock and the state check together make a
  double firing impossible, even with two API processes.
- **A schedule only requests a plan.** The plan waits for a human's approval like any other (Phase 5 Q3).
  Letting a schedule create tasks directly would be the first thing in Axiom that runs without a human's yes,
  and needs its own ADR.
- **Missed occurrences fire once,** on the first tick after downtime, and the next occurrence is computed from
  now. They're not replayed.
- **Budgets apply as always.** A scheduled plan that would go over the day's budget is refused, and the refusal
  is an event and a notification (ADR-019), never a silent skip.

## Consequences
- Axiom can run unattended without a person starting each piece of work, and still nothing is built without a
  human approving its plan, and nothing is merged without a human.
- Timer firings are events, so the metrics and report can tell scheduled work from requested work.
- A schedule is standing permission to *spend* (on planning) without asking each time. Its cost is capped by the
  daily budget, and each schedule is visible and cancellable on the dashboard.
