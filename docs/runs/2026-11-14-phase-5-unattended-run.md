# Phase 5 T5b: a scheduled run on tiny-agenticsoc, Nov 14–15 2026

**Question:** does Axiom run on its own between a human's decisions: a schedule fires on time, the human is told
when they're needed, and nothing fires twice, even across a restart? **Short answer:** yes. A weekly schedule
requested a plan on time, notifications reached the inbox issue, both tasks passed review and CI on the first round,
and a schedule due while the API was down fired once at startup.

## Setup

- `make up`, `make workers` (migrations 0009 timers and 0010 notifications applied at startup).
- `make check-boundaries`: every boundary held in the worker and sync containers. Only `github-sync` holds
  `AXIOM_SYNC_GITHUB_TOKEN` (checked in four containers).
- Token probes (an empty `POST`; 422 = allowed, 403 = refused). Sync token: issues 422; contents, pulls and another
  repository's issues 403. **Workers' token: issues 422**, a permission they don't need (see Findings).
- Schedule 1: weekly, budget $6, *"Find the least-tested module in agent/socagent and add focused tests for its
  untested behaviour. No production code changes."*

## The scheduled run

From the event log (UTC, 14 November):

| Time | Actor | Event |
|---|---|---|
| 16:11:11 | human:bharath | schedule created, first due 16:14:11 |
| 16:14:11 | system:scheduler | PLAN_REQUESTED (plan 3), TIMER_FIRED |
| 16:14:14 | agent:EM-01 | PLAN_CLAIMED |
| 16:15:15 | agent:EM-01 | PLAN_PROPOSED, $0.24; follow-up set; notification "plan 3 is ready for your approval." |
| 16:20:17 | human:bharath | PLAN_APPROVED: SOC-011, SOC-012 created; follow-up cancelled |
| 16:20:24 | agent:QA-01 | SOC-011 claimed and started |
| 16:22:36 | agent:QA-01 | SOC-011 submitted ($0.43), PR #14; SOC-012 claimed |
| 16:23:36 | system:aggregator | SOC-011 approved (security PASS $0.07, code PASS $0.53); follow-up and notification |
| 16:24:30 | agent:QA-01 | SOC-012 submitted ($0.37), PR #15 |
| 16:25:36 | system:aggregator | SOC-012 approved (security PASS $0.06, code PASS $0.48); follow-up and notification |
| 17:12:53 | human:github-bharathcmbox | both merged: DONE; follow-ups cancelled |

- **About eleven minutes of agent work, $2.18** (plan $0.24, SOC-011 $1.04, SOC-012 $0.91). Nearly all the elapsed
  time was the human: five minutes to approve, forty-seven to merge.
- **Notifications:** the sync opened [issue #13 "Axiom inbox"](https://github.com/bharathcmbox/tiny-agenticsoc/issues/13)
  and posted three comments, fixed text only: "plan 3 is ready for your approval." and "SOC-011 / SOC-012 passed
  review and CI: its pull request is ready to merge."
- The two QA tasks could have run in parallel, but there is one QA agent, so they ran one after the other.

## The restart drill

To check "fires once, even after downtime" without paying for a plan: the planner was stopped, schedule 5 created
due at 04:24:11 (15 November), and the API stopped at 04:22:42 and started at 04:25:41.

| Time | Actor | Event |
|---|---|---|
| 04:25:41 | system:scheduler | at startup, one second after the API came up: PLAN_REQUESTED (plan 4), TIMER_FIRED, `missed: 0`, next due 2026-11-16 04:24:11 |

It fired once, and its next time stayed on the schedule's own grid. Plan 4 and schedule 5 were then cancelled by the
human before any planning: **$0**.

## Findings

1. **GitHub doesn't email people about their own activity.** The sync token is the human's own, so comments reached
   the issue but not their inbox, unless they turn on *Include your own updates*. Documented in ADR-019, chapter 17
   and the admin guide; a machine account's token would avoid it.
2. **The workers' GitHub token can create issues.** It predates Phase 5 and nothing used it, but a compromised worker
   could have posted fake inbox comments. Workers' PR comments work with Pull requests write, so Issues should be
   removed from that token (the human's action). The docs had claimed otherwise; corrected in `3bf2000`.

## Still to come

The week isn't over: the schedule's next firing is 2026-11-21. A reminder firing for real needs something left
waiting for a day; none was. Both are covered by tests, and are worth recording here when they happen.
