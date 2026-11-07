# Phase 4 exit run (T10), Nov 7 2026

A real multi-task spec on `bharathcmbox/tiny-agenticsoc`, the [exit spec](../plan/phase-4.md#7-exit-spec-for-tiny-agenticsoc):
planned by EM-01, approved by the human as proposed, developed and reviewed by agents, every PR merged by the
human on GitHub. All numbers below come from the event log and the metered costs (ADR-015), not from memory.

## Result

**Delivered.** Plan 2's five tasks are DONE, all merged after the gate (none `merged_before_gate`), with no rework
round and no merge conflict. One task went to the human once, for budget, not for quality.

| Task | Role | Agent | Work | PR | Developer | Reviews | Total / budget |
|---|---|---|---|---|---|---|---|
| SOC-006 | developer | DEV-01 | `investigate` uses `investigation_queue`, `--min-severity`; ledger totals per model and provider | [#8](https://github.com/bharathcmbox/tiny-agenticsoc/pull/8) | $0.29 | $0.89 | $1.18 / $2.50 |
| SOC-007 | developer | DEV-02 | OpenRouter: no forced `tool_choice` where rejected, prompt caching | [#9](https://github.com/bharathcmbox/tiny-agenticsoc/pull/9) | $0.39 | $0.87 | $1.26 / $3.00 |
| SOC-008 | developer | DEV-01 | Docs for both providers (incl. one `CLAUDE.md` bullet) | [#11](https://github.com/bharathcmbox/tiny-agenticsoc/pull/11) | $0.55 | $1.03 | $1.58 / $2.00 |
| SOC-009 | qa | QA-01 | Tests for `investigate` and `ledger` | [#10](https://github.com/bharathcmbox/tiny-agenticsoc/pull/10) | $0.51 | $1.99 | $2.50 / $3.50 (raised from $2.50) |
| SOC-010 | qa | QA-01 | Tests for the OpenRouter path | [#12](https://github.com/bharathcmbox/tiny-agenticsoc/pull/12) | $0.32 | $1.00 | $1.32 / $2.50 |
| Plan 2 | planner | EM-01 | 5 tasks proposed, approved as is | — | $0.45 | — | $0.45 |
| **Total** | | | | | **$2.51** | **$5.78** | **$8.29** |

The spec allowed $15 for the exit plan; the plan budgeted $12.50 for its tasks and used $7.84 of it.

## Time

From the plan request to the last merge: 65 minutes (16:46 to 17:51 UTC). Agents worked about 2 minutes per
task; review and CI took 1 to 2 more. Everything else was waiting for the human: reading the plan, reading
each PR, merging.

| Task | Developer ran | To a ready PR | Waiting for the human to merge |
|---|---|---|---|
| SOC-006 | 17:01:27–17:02:51 | 2 m 23 s | 13 m 44 s |
| SOC-007 | 17:01:28–17:03:23 | 3 m 18 s | 13 m 20 s |
| SOC-008 | 17:18:11–17:19:59 | 3 m 54 s | 27 m 19 s |
| SOC-009 | 17:17:39–17:19:53 | escalated at 17:21:13; ready after the rereview at 17:47:33 | 4 m 01 s after it was ready |
| SOC-010 | 17:19:53–17:21:33 | 3 m 07 s | 26 m 59 s |

**Parallelism was real.** SOC-006 and SOC-007 started one second apart on two developers. SOC-008 and SOC-009
ran at the same time once their dependencies were merged, and SOC-010's QA agent ran while SOC-009 was in review.

## Reviews

Eleven reviews were requested, ten submitted: all PASS, no blocking finding. The findings, all `low`, were real
but small: a dead branch, an inaccurate docstring, a redundant assertion, a vacuous one, and that the developer
tasks added no tests (by design: the plan gave tests to QA tasks).

One review failed: SOC-009's code review hit its $1.00 run cap at $0.83 on 537 lines of new tests. As designed,
it went to the human at once, without a retry. The human raised the task to $3.50 and issued `rereview`. The
security PASS was carried forward on the same commit (`REVIEW_CARRIED_FORWARD`), and only the code review ran
again, passing at $0.72. SOC-008's code review waited a minute while other runs held reservations: the
designed "wait, spend nothing" path.

## What the run taught

- **The review run cap is fixed at $1.00** (`review_budget_usd`), whatever the task has left. Raising the
  task budget let the rereview start but did not give it more room. It fitted this time ($0.72); a larger
  PR could fail twice. Fixed after the run: a review now gets its share of what the task has left, from $1 up
  to $2.50 (ADR-013, revision 2).
- **Reviews are 74% of the plan's task cost** ($5.78 of $7.84), as in every earlier run. Two reviewers on
  every PR, including docs-only and tests-only ones, is the main cost.
- **The human is the bottleneck by about tenfold:** about 3 minutes of agent time per task against 13 to 27
  minutes waiting for a merge.
- **Not verified live:** the OpenRouter change (#9) and its tests (#12) were checked only offline. Whether
  Sonnet 5.5 and Opus 5.5 really reject a forced `tool_choice` on OpenRouter is still an assumption; Fable 5.1
  isn't on the list, and would fall back on the run-time 400 handling.
- **Security:** every workspace (1,729 files) and every PR's text and diff were scanned for the values in
  `.env`; none found. The `CLAUDE.md` edit in #11 was read line by line: it changes only the model-access
  bullet, and adds no instruction, URL or command.

## The daily report, after the run

```text
## Delivery so far (project SOC, demo tasks excluded)

- Tasks: 8; merged: 8
- Cost per merged task (all spend): $1.40
- First-pass acceptance: 75%
- Rework rounds per merged task: 0.25
- Median cycle time: 32m 11s
- Median waiting for a human: 20m 21s (elapsed time, an upper bound on attention)
- Human interventions per merged task: 0.625
- Reviews' share of cost: 76%
- Review verdicts: {'PASS': 15} (a FAIL may be a false alarm)
- Defects found after merge: 0
- Merged before the gate: 2
```

These cover plans 1 and 2 and the earlier real tasks. Eight tasks are a small sample, and there is still no
hand-driven baseline to compare them with, so they describe the system, not whether it beats one session.
