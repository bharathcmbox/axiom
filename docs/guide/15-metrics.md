# 15. Metrics: the measured answer

SPEC section 1 says success is "a measured answer, not opinion". This chapter shows where the
numbers come from and how far to trust them.

## One source of truth

```mermaid
flowchart LR
    ev[("events<br/>append-only")] --> m["metrics.py<br/>pure functions"]
    ex[("executions, reviews<br/>cost")] --> m
    you["you record:<br/>defects, baselines"] -->|"as events"| ev
    m --> api["GET /metrics"]
    m --> rep["daily report"]
    m --> dash["dashboard"]
```

Nothing is stored as a counter (ADR-012). Metrics are recomputed from the record every time, so a
corrected definition also corrects history, and each definition has unit tests on scripted histories.

## How a task's time is divided

Each transition event says when the task entered a state; the next one says when it left. Every
second between READY and DONE lands in exactly one bucket:

```mermaid
flowchart LR
    subgraph q["queue_s"]
        PENDING
        READY
    end
    subgraph a["agent_s"]
        ASSIGNED
        IN_PROGRESS
    end
    subgraph r["review_s"]
        IN_REVIEW
    end
    subgraph h["waiting_for_human_s"]
        AWAITING_MERGE
        WAITING_FOR_HUMAN
        FAILED
    end
    q --> a --> r --> h
```

`test_task_durations_add_up` proves the buckets add up to the cycle time.

## The metrics

| Metric | Definition | Trust it for |
|---|---|---|
| `cycle_s` | first READY → DONE | How long delivery took, including waiting for you |
| `agent_s`, `review_s` | time in those states | What the machines cost in time |
| `waiting_for_human_s` | AWAITING_MERGE + WAITING_FOR_HUMAN + FAILED | **An upper bound.** It's elapsed time: you may have been asleep |
| `first_pass_rate` | merged with `review_rounds = 0` | How often work is right first time |
| `rework_rounds_per_merged` | average `review_rounds` | How much the review loop is used |
| `failed_attempts` | EXECUTION_FAILED + EXECUTION_TIMED_OUT | Reliability of runs |
| `human_interventions` | events by `human:` actors, except creating, merging, tagging and recording defects | How unattended it really is |
| `cost_per_merged_usd` | **all** spend, including failed tasks, ÷ merged tasks | What delivery costs. Not cost per run |
| `review_share_of_cost` | reviews ÷ total | Whether reviewers are worth a look |
| `verdicts` | counts of PASS / FAIL | **Not quality:** a FAIL may be a false alarm |
| `defects_after_merge` | DEFECT_RECORDED | Only as good as your recording |
| `merged_before_gate` | merges flagged by the sync | How often the gate was skipped |

## Recording what only you know

```bash
curl "${H[@]}" localhost:8000/tasks/SOC-002/defects -d '{"description": "…", "severity": "high"}'
curl "${H[@]}" localhost:8000/baselines -d '{"project_key": "SOC", "title": "similar task by hand",
     "minutes": 45, "cost_usd": "0.80", "notes": "one Claude Code session"}'
curl "${H[@]}" localhost:8000/tasks/SOC-002/tags -d '{"tags": ["demo", "planted"]}'
```

**Baselines answer SPEC's main question:** does this beat you driving one session? Without a
comparable hand-driven task there is nothing to compare with. Collecting them is part of Phase 5.

**Demo work is left out by default.** Planted and demo tasks are tagged `demo`, so they can't
flatter or distort the results: `make demo-restart` tags its own. Add `?include_demo=true` to see
them. This matters more than it sounds. Before the old demo tasks were tagged, the first report
said "$0.03 per merged task, 100% first-pass", from tasks no agent ever worked on.

## How far to trust a cost

A run reports its cost in its final message, and that figure is what the SDK says was billed. A
killed run never sends one. All that's left is its transcript, so Axiom estimates, and marks the
result `cost_estimated = true`. The dashboard shows it as `~$0.14 est.`.

```mermaid
flowchart TB
    r["A run ended"] --> f{"Final report?"}
    f -->|yes| real["Reported cost"]
    f -->|"no: timeout, lost lease"| e["Harness reads transcript.jsonl"]
    f -->|"no: worker killed"| w["Worker restarts,<br/>finds the workspace it left"]
    w --> e
    e --> s{"What does it show?"}
    s -->|"final message present"| sdk["The SDK's own figure"]
    s -->|"only per-message usage"| calc["Input + cache: exact<br/>Output: visible text / 4 x 2.5"]
    sdk --> mark["Recorded as an estimate.<br/>The first report wins, so a repeat can't double-count."]
    calc --> mark
```

The output figure needs scaling because mid-run messages carry a running count (120 tokens where
2,481 were billed), and thinking is billed but not shown. Replaying real runs with the final report
removed:

| Run | Actually cost | Usage alone | Estimate |
|---|---|---|---|
| SOC-001 | $0.1459 | $0.0987 | $0.1459 |
| SOC-002 developer, round 2 | $0.1722 | $0.1083 | $0.1797 |
| SOC-002 code review, round 1 | $0.2585 | $0.1618 | $0.2790 |
| SOC-002 security review, round 1 | $0.3042 | $0.1952 | $0.3144 |
| SOC-002 code review, round 2 | $0.2451 | $0.1817 | $0.2522 |
| SOC-002 security review, round 2 | $0.2133 | $0.1619 | $0.2228 |
| **Total** | **$1.3393** | **$0.9076** | **$1.3940 (+4%)** |

**Read this table with care.** The 2.5 was chosen from these same six runs, so it shows the method
fits them, not that it predicts new ones. A run killed early may differ. It errs high on purpose,
because a budget should overstate rather than understate. Prices live in `runtimes/estimate.py`
and must be updated if they change; `test_prices_reproduce_a_real_billed_run` checks them against a
real bill. The Anthropic Console remains the authority on what you were charged.

A review retried after an earlier attempt already reported a cost keeps that first figure, so a run
lost on top of an earlier failure can still be undercounted.

## The daily report

`make report` (or `make report PROJECT=SOC`) prints Markdown: plain numbers, no LLM (Q4 in the
plan), so it costs nothing and can't invent anything. It leads with what needs you, then today's
spend and activity, delivery so far, and baselines.

## The dashboard

The same data as HTML, served by the API itself at <http://127.0.0.1:8000/ui>: no new service.

```mermaid
flowchart TB
    b["Your browser"] -->|"GET /ui"| c{"valid session cookie?"}
    c -->|no| l["/ui/login: paste an API token"]
    l -->|"POST: token checked"| s["cookie set: HttpOnly, SameSite=Strict"]
    s --> c
    c -->|yes| o["<b>Overview</b><br/>needs you, numbers, tasks"]
    o --> t["<b>One task</b><br/>spec, reviews + findings,<br/>developer runs, full timeline"]
    t -.-> x["Nothing here changes a task:<br/>merging stays on GitHub,<br/>commands stay on the API"]
```

Three rules, each with a test:

- **Nothing on it can change state.** Every route is GET except the login form, which only checks a
  token and sets a cookie (`test_only_login_is_not_a_get`). The plan said "every route is GET"; a
  browser can't send a bearer header, and a token in a URL would land in logs and history, so login
  is the one exception.
- **Everything shown is treated as hostile.** Titles, specs, findings and errors are written by
  models or found in repositories, so all of it is escaped, and only `https://` links are rendered
  (`test_untrusted_text_is_escaped_everywhere`).
- **Demo work stays out** unless you ask (`test_demo_tasks_are_hidden_unless_asked`).

The cookie holds your API token until you close the browser. It isn't marked `Secure` because this
is plain HTTP on 127.0.0.1; it would need to be before the API ever left localhost. To end access,
revoke the token.

## The first real numbers

SOC-002, the planted-bug run (so tagged `demo`), computed entirely from its event log, reconciling
exactly with what was recorded by hand:

| | |
|---|---|
| Developer | $0.1722 |
| Reviews (4) | $1.0211 |
| **Total** | **$1.1933** |
| Rework rounds | 1 |
| Failed attempts | 2 (Axiom's own sandbox bug) |
| Human interventions | 1 (the `retry` after fixing it) |
| Agents working | 92 s |
| Reviews | 90 s |
| Waiting for a human | about 71 min |

Two early hints, from a sample of one: **reviews are most of the cost** (86% here), and **the human
is the bottleneck by a factor of over 20.** Both are exactly what SPEC wanted to find out, and both
need more data before they mean much.

## The exit run's numbers

Phase 4's exit run (a five-task plan, [run record](../runs/2026-11-07-phase-4-exit-run.md)) gave the same two
answers on more data:

| | Plan 2 |
|---|---|
| Tasks merged | 5 of 5, none before the gate |
| Plan + developers + reviews | $0.45 + $2.06 + $5.78 = **$8.29** |
| Reviews' share of task cost | 74% |
| Rework rounds | 0 |
| Escalations | 1 (a review ran out of its run cap) |
| Agents' time to a ready PR | 2 m 23 s to 3 m 54 s |
| Waiting for you to merge | 13 to 27 minutes |

Reviews are still about three quarters of the cost, and you're still the bottleneck, now by about tenfold. The
report after the run (all real SOC tasks, eight in all) gives $1.40 per merged task and 75% first-pass
acceptance. There's no hand-driven baseline yet, so these numbers can't say whether agents beat one session.

## Try it

1. `curl "${H[@]}" "localhost:8000/metrics?project=SOC&include_demo=true" | python3 -m json.tool`
2. `uv run pytest tests/unit/test_metrics.py -v`: every definition, on a scripted history.
3. Change a definition in `metrics.py` (say, count FAILED as agent time) and see which test objects.
4. `make report PROJECT=SOC`, then open <http://127.0.0.1:8000/ui> and follow a task to its timeline.
   Issue a token to sign in with: `docker compose exec api python -m axiom.registry.tokens issue human:you`.
