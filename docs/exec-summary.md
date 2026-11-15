# Axiom: executive summary

*November 2026, at the end of Phase 5.*

## What it is

Axiom runs a small team of AI coding agents on real software work, with a human as CTO. You give it a
specification. A planner agent splits it into tasks, and you approve the plan. Developer and QA agents implement
the tasks in locked-down sandboxes. Two reviewer agents, on a different model, judge every change with your CI.
**You merge. Axiom never does.** Every decision about what happens next is made by plain, tested code, never by a
model, and every step is recorded in an append-only event log.

```mermaid
flowchart LR
    s(["Spec"]) --> p["Plan"] --> a(["You approve"]) --> d["Agents build,<br/>in parallel"]
    d --> r["Reviews + CI"] --> m(["You merge"])
    r -->|"findings"| d
```

## Where it stands

All five phases are built and proven on a real repository (`tiny-agenticsoc`), with one Phase 5 item deferred:

| Phase | Proven by |
|---|---|
| 1. Control plane | Tasks driven through every state; killed and restarted mid-work with nothing lost |
| 2. First worker | A real task went from READY to an open PR with no human input |
| 3. Quality | A planted one-character bug passed every test and CI; a reviewer caught it; an agent fixed it on the same PR |
| 4. Engineering management | A five-task spec planned, approved, built in parallel, reviewed and merged end to end |
| 5. Always-on | A weekly schedule requested a plan on its own; a feature was delivered with Axiom waiting quietly between your approvals and merges; notifications reached you on GitHub; a restart didn't make a schedule fire twice; the same reviews ran on a second runtime (Managed Agents). Hand-driven baselines deferred |

## Results so far

| Measure | Value |
|---|---|
| Real tasks merged | 8, with no defect recorded after merge |
| Cost per merged task, everything included | $1.40 |
| First-pass review acceptance | 75% |
| A five-task spec, request to last merge | 65 minutes, $8.29 |
| Agent time per task | about 3 minutes |
| Waiting for the human to merge | 13–27 minutes per PR |
| Reviews' share of cost | about three quarters |

**Phase 5 added:** a scheduled run (two tasks, $2.18, about eleven minutes of agent work) and a feature with a
dependency between its tasks ($2.42, about seven minutes of agent work over nineteen hours, the rest waiting for
you); all eight Phase 5 reviews passed on the first round. Reviewers now run on two different models: code review
on Claude Fable 5.2, security on Claude Sonnet 5.5, both with checklists a review must cover.

**Two findings stand out.** The human is the bottleneck, by about tenfold. And reviews are most of the cost:
Claude Sonnet 5.5 as a reviewer cost about a seventh of Claude Fable 5.1 and agreed on every verdict, including
catching the planted bug. But it missed about half of Fable's smaller findings on test quality
([comparison](runs/2026-11-07-reviewer-comparison.md)). The reviewer model is now a one-line setting per role.

## Security

The model never holds a credential. It runs in a sandbox that reaches the model only through a metered proxy
holding the API key, and other sites only through an allowlist. GitHub and the Docker socket are held by separate
services.

```mermaid
sequenceDiagram
    participant W as Worker<br/>holds the GitHub token
    participant S as Sandbox, where the model runs<br/>holds no credentials
    participant P as Model proxy<br/>holds the Anthropic key
    participant A as Anthropic
    participant G as GitHub
    W->>P: open a run: one model, one budget
    P-->>W: a run token, valid for minutes
    W->>S: start, with only the run token
    S->>P: model request + run token
    Note over P: checks the token's model, budget and expiry,<br/>removes it, adds the real key
    P->>A: model request + API key
    A-->>P: reply
    P-->>S: reply, with its cost metered
    S-->>W: changed files, nothing else
    Note over W: checks the files: allowed paths,<br/>no secrets, tests pass on a clean copy
    W->>G: push an axiom/* branch with the GitHub token
```

A stolen run token is worth one model and one budget for a few minutes, and works only from inside Axiom's internal
networks: the proxy isn't reachable from outside.
The sandbox has no route to GitHub at all: only the worker pushes, after checking the work.

A prompt-injection drill was refused at every step. Preparing for it found a real sandbox escape (through
git), which was fixed before it could be used. A live check (`make check-boundaries`) proves 33 limits from
inside real containers. The largest remaining exposure is GitHub itself: a compromised agent could push harmful
code to its branch. Reviews, CI and a human merge stand between that branch and `main`.

## Cost to build

The API cost of building and proving all five phases was about $29: under $2 for Phases 1–3, $21.79 for Phase 4,
and $7.17 for Phase 5 (live review checks, the scheduled run, the feature run and the Managed Agents spike),
within caps of $35 and $40. Every figure comes from the
metered proxy and the event log.

## Limits and risks

- **Small sample.** Eight tasks, one planted bug. The numbers describe the system, not yet its quality at scale.
- **The main question is open.** Whether agents beat one person with one AI session needs hand-driven baselines.
  Phase 5 deferred them.
- **One machine, one operator.** The API is local, and GitHub's own merge protection isn't enforced yet.
- **Unverified paths.** The OpenRouter provider change in `tiny-agenticsoc` was tested offline only.

## Next

- **Baselines** (deferred from Phase 5): a few tasks done by hand in one Claude Code session each, to answer the
  main question.
- **A month unattended:** the weekly schedule keeps running; the first reminders firing for real.
- **Managed Agents for reviews,** once the session driver's Anthropic key fits ADR-015 (ADR-020).
- **A second QA agent,** since one QA agent made parallel test tasks wait.

More: [user guide](user-guide.md) · [admin guide](admin-guide.md) · [the guide](guide/README.md) ·
[Phase 5 exit run](runs/2026-11-15-phase-5-exit-run.md) · [SPEC](../SPEC.md)
