# Phase 5 T8: a feature delivered while Axiom ran unattended, Nov 15 2026

**Question:** with schedules and notifications on, does a real multi-task spec go from a request to merged work,
with Axiom waiting quietly between the human's decisions? **Short answer:** yes. Two tasks, a dependency between
them, both reviews passing on the first round, and $2.42 in all. The run took nineteen hours, almost all of it the
human's; the agents worked for about seven minutes.

## The spec

> Guest token expiry and a spend report for the console's access control (agent/socagent/access.py): `token add
> --days N`; expired tokens refused like revoked ones; old token files still load; the owner never expires; expiry
> shown in `token list`; a read-only `socagent spend [--days 7]`; tests for access.py, which had none, with every path
> in a temporary directory; the README updated. Budget $12.

The human asked for the plan and approved it in this session, on their explicit instruction ("go ahead with the
token expiry spec"). Both are recorded as `human:bharath`.

## The plan

Proposed for $0.26: two tasks, $9.50 of the $12. SOC-013 (developer: `access.py`, `__main__.py`, `README.md`, $5)
and SOC-014 (QA: `agent/tests/test_access.py`, $4.50, depending on SOC-013). The planner fixed a few things the spec
left open (an expired name can be re-added; `--days` must be at least 1; the report covers today and the N−1 days
before it) and said so in its risks.

## Timeline (UTC, 15 November)

| Time | Actor | Event |
|---|---|---|
| 04:26:13 | human:bharath | PLAN_REQUESTED (plan 5) |
| 04:27:36 | agent:EM-01 | PLAN_PROPOSED, $0.26 |
| 04:34:24 | human:bharath | PLAN_APPROVED: SOC-013 READY, SOC-014 PENDING (waits for SOC-013) |
| 04:34:31 | agent:DEV-02 | SOC-013 claimed and started |
| 04:36:10 | agent:DEV-02 | submitted, $0.33, PR #16 (`access.py` +49/−6, `__main__.py` +27/−5, README +6/−2) |
| 04:37:17 | system:aggregator | approved: security PASS $0.07, code PASS $0.55, CI green; notification posted |
| 23:18:51 | human:github-bharathcmbox | PR #16 merged: SOC-013 DONE, SOC-014 promoted to READY |
| 23:18:53 | agent:QA-01 | SOC-014 claimed and started, two seconds after the merge |
| 23:21:02 | agent:QA-01 | submitted, $0.42, PR #17 (`test_access.py`, +465; 228 tests passed in verification) |
| 23:22:16 | system:aggregator | approved: security PASS $0.09, code PASS $0.70 with three low findings, CI green |

## Reviews

- **SOC-013, security (Sonnet 5.5):** no findings. Expired guests are refused through the same path as revoked ones;
  the owner is exempt by role; `--days` is validated before anything is created; expiry fails closed (a malformed or
  timezone-naive date raises instead of admitting the token).
- **SOC-013, code (Fable 5.2):** no findings, after tracing expiry, the owner exemption and the report against the
  spec.
- **SOC-014, code (Fable 5.2):** three low, non-blocking findings: date-based tests read the real clock (could fail
  across midnight UTC); the guard against touching real files would trip if the console wrote at the same time; the
  half-cent budget boundary (`<=` against `<`) isn't pinned exactly.
- **SOC-014, security (Sonnet 5.5):** no findings; confirmed every path is redirected to a temporary directory.

## Cost

| Item | Cost |
|---|---|
| Plan | $0.26 |
| SOC-013 (developer + 2 reviews) | $0.95 |
| SOC-014 (QA + 2 reviews) | $1.21 |
| **Total** | **$2.42** |

## What it shows

- Dependencies wait for the human's merge, then move at once: the dependent task started two seconds after it.
- Nothing ran while waiting: no polling cost, no retries, no reminders needed (the merge came within the two days).
- Every human decision is in the event log as the human: the plan approval and both merges.
