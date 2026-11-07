# ADR-013: Run budgets are reserved at claim time

Status: Accepted (2026-11-06)

## Context
The Phase 4 plan said the daily check "isn't serialized" and proposed an advisory lock around the
check-and-claim. A lock alone doesn't help: a claim doesn't change what has been spent, because a
run's cost is only known when it ends. Two claims, one after the other, would read the same money
and each be told they may spend it. The same applied to two reviewers on one task.

## Decision
- A claim **reserves** its run budget, stored on the execution or review as `run_budget_usd`.
- "Left" means budget − spent − what active runs have reserved, for both the task and the day.
- `budgets.reserve` does the check under an advisory lock, in the claim's transaction. Lock order:
  task → agent → developer cap → budget.
- A reservation ends when the run ends: unspent money is free again at once.
- One developer run may reserve at most `max_run_budget_usd` ($6), so a task with a large budget
  can't hold the whole day.
- `spent` and `reserved` mean different things when a claim can't start. Spent: a task whose reviews
  can't happen escalates to a human, because waiting can't help. Reserved: the claim is refused and
  retried, because the money may come back.

## Consequences
- With the runs' own hard stops, spend can't exceed the day's cap by more than those stops overshoot.
- Parallel work can be refused while money is merely reserved, so a small daily cap limits
  parallelism. The defaults fit two developers ($6 each) and two reviewers ($1 each) in $15.
- A killed run's reservation ends when its lease expires, and its cost is unknown until it is
  estimated (chapter 15). A sandbox orphaned by a killed worker can keep spending, up to its own
  budget stop, after the reservation has ended. That is a known limit.

## Revision (2026-11-07)
Reservations alone could hand a run too little to finish. In the first real QA run, one reviewer reserved $1 of
$1.28, the second was given $0.28 for work costing about $0.40, and it was retried three times with less. Reviews and
planning runs now have a minimum. Below it a claim waits if money may come back, and a task that can't pay goes to a
human before spending. A run that runs out of budget isn't retried. Developer runs have no minimum yet.

## Revision 2 (2026-11-07, after the exit run)
A review run's cap was fixed at $1, so raising a task's budget let a rereview start but gave it no more room: the
exit run's SOC-009 code review ran out at $0.83 on 537 lines of tests. A review now asks for a fair share of what the
task has left, split between the reviews of its round still waiting to start, at least `review_budget_usd` ($1) and
at most `review_max_run_usd` ($2.50). `reserve` still clamps it to what the task and the day have free, so the share
only decides how the money is split, never how much exists. Reviews use only what they need, so a larger cap costs
nothing unless a review needs it. Splitting by the reviews still waiting keeps the first from taking the second's
money, the SOC-005 failure.

