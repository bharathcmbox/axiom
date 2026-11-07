---
title: "Axiom: what I learned building a small AI engineering organization"
date: 2026-11-07
draft: false
description: "A learning project: a control plane that runs a small team of AI coding agents on a real repository, with a human as CTO. What it solves, how it differs from Claude Code and Codex, how it was built, and where it falls short."
tags: [ai-agents, claude, software-engineering, security, learning]
---

> **This is a learning project.** Axiom is not a product, not a service, and not ready for anyone's
> production code. I built it to understand, hands-on, what it takes to let AI coding agents do real
> work safely: who decides, who checks, what it costs, and where it breaks. Everything below is written
> in that spirit, including the parts that didn't work.

<!--more-->

## The problem

Coding agents are now good at a task you hand them in a session you're watching. You read what they do,
you correct them, you decide when it's done.

Things change when you want several agents to work on a real codebase without you approving every step:

- **Who decides what happens next?** If a model decides that work is finished, reviewed and ready to
  merge, a confident mistake goes straight through.
- **Who checks the work?** An agent reviewing its own work, or a twin of itself, isn't much of a check.
- **What does it cost?** A loop that retries "until it passes" can spend money without limit.
- **What can a misled agent reach?** An agent that reads a malicious comment in a repository shouldn't
  be able to hand your API keys to anyone.
- **Did it actually help?** Without measurements, "the agents did it" is an anecdote, not a result.

Axiom is my attempt to answer those questions in code: a control plane that runs a small team of
coding agents on a real repository, with me as the CTO.

## What Axiom does

You give Axiom a specification. A planner agent reads the repository and proposes a set of tasks.
Plain code checks the plan, and you approve it. Developer and QA agents then implement the tasks in
locked-down sandboxes, in parallel where the work is independent. Two reviewer agents, running on a
different model, judge each change alongside your CI. Findings go back to the developer on the same
pull request. When everything passes, **you merge on GitHub. Axiom never merges.**

```mermaid
flowchart TB
    spec(["You: a specification and a budget"]) --> plan["Planner agent proposes tasks"]
    plan --> rules{"Plan rules<br/>(plain code)"}
    rules -->|broken| plan
    rules -->|ok| approve(["You approve the plan"])
    approve --> dev["Developer and QA agents<br/>in sandboxes, in parallel"]
    dev --> check{"Axiom checks:<br/>allowed files? no secrets?<br/>tests pass on a clean copy?"}
    check -->|no| dev
    check -->|yes| pr["Pull request"]
    pr --> rev["Two reviewers, another model"]
    pr --> ci["CI"]
    rev --> agg{"Aggregator<br/>(plain code)"}
    ci --> agg
    agg -->|findings| dev
    agg -->|stuck| you2(["You decide"])
    agg -->|all pass| merge(["You merge"])
```

A few rules shaped everything:

1. **A model gives opinions; code makes decisions.** Every state change goes through hand-written
   state machines and one function that applies them. A reviewer's verdict is an input. The decision to
   approve, send back or escalate is made by a small, tested aggregator.
2. **Nobody approves their own work.** No agent can approve. Reviewers run read-only on a different
   model from the developers, and the system refuses a configuration that breaks this.
3. **Agents hold no credentials.** More on this below.
4. **Budgets stop work; they don't just report it.** Money is reserved before a run starts, per run,
   per task and per day.
5. **Everything is recorded.** All state lives in PostgreSQL, and every change writes an event to an
   append-only log in the same transaction. Killing the process loses nothing.

## How it differs from Claude Code and Codex

Axiom doesn't compete with coding agents; it uses one. Claude Code (through the Claude Agent SDK) does the
actual work inside Axiom's sandboxes. Claude Code and Codex are the *workers*. Axiom is an experiment in
the *organization* around them.

| | A coding agent (Claude Code, Codex) | Axiom |
|---|---|---|
| Unit of work | A task in a session | A specification, split into tasks with dependencies |
| Who drives | You, interactively | A backlog and state machines; you approve plans and merge |
| Who checks | You, as you go | Independent reviewers on another model, re-run tests, CI, then you |
| What decides "done" | You, or the agent | Plain code, from verdicts and CI on the exact commit reviewed |
| Credentials | Whatever the session can reach | None in the sandbox; separate services hold each secret |
| Cost control | Per session | Reserved before spending: per run, per task, per day |
| Record | The conversation | An append-only event log, with metrics computed from it |

The point isn't that coding agents lack these things. Many teams build them around agents in their own
way. The point was to build them myself, see which ones mattered, and measure them.

## How the sandbox never sees a credential

This part taught me the most. The model runs in a container that holds **no** credentials. Two things it
needs are handled outside it:

```mermaid
sequenceDiagram
    participant W as Worker<br/>holds the GitHub token
    participant S as Sandbox, where the model runs<br/>holds no credentials
    participant P as Model proxy<br/>holds the API key
    participant A as Model API
    participant G as GitHub
    W->>P: open a run: one model, one budget
    P-->>W: a run token, valid for minutes
    W->>S: start, with only the run token
    S->>P: model request + run token
    Note over P: checks model, budget and expiry,<br/>swaps the token for the real key
    P->>A: request + API key
    A-->>P: reply
    P-->>S: reply, cost metered
    S-->>W: changed files only
    Note over W: checks the files, re-runs the tests
    W->>G: pushes an axiom/* branch
```

- **The model API:** a small proxy is the only holder of the API key. The sandbox gets a short-lived run
  token that works for one model, under one budget, for a few minutes. The proxy swaps it for the real key
  on the way out, and meters what the run costs. That metered cost is the record. Whatever the sandbox
  claims, it isn't trusted.
- **GitHub:** the sandbox has no route to GitHub at all. A deterministic worker outside the sandbox holds
  the token. It checks the changed files (allowed paths, no secrets, no symlinks, tests passing on a clean
  copy) and only then pushes, to `axiom/*` branches only. There is no code path that merges.
- **Everything else:** a separate launcher is the only service with the Docker socket. Internal networks
  keep workers away from the database, and keep sandboxes away from everything except the model proxy and
  a package-index proxy.

## How I built it

I built Axiom in four phases, each ending with a demonstration on a real (small) repository of my own:

| Phase | What it added | How it was proven |
|---|---|---|
| 1. Control plane | Database schema, state machines, leases, event log, REST API | Every legal and illegal transition tested; killed mid-work and restarted with nothing lost |
| 2. First worker | Sandbox, worker, verification, pull requests, budgets | A real task went from "ready" to an open PR with no human input |
| 3. Quality | Independent reviewers, the aggregator, CI gate, rework on the same PR, GitHub sync | A planted one-character bug passed every test and CI; a reviewer caught it; an agent fixed it |
| 4. Engineering management | Planner and plan rules, QA agents, parallel developers, metrics, dashboard, hardening | A five-task specification planned, approved, built in parallel, reviewed and merged end to end |

The stack is deliberately plain: Python, FastAPI, PostgreSQL with raw SQL (no ORM), Docker Compose, and
pytest against a real database. I wrote it with Claude Code as a pair programmer, under a strict loop:
one small task at a time; code, tests and documentation together; lint and the full test suite before every
commit; one commit per task. Every transition needs a test for the legal path *and* a test proving that an
illegal attempt changes nothing and writes no event. The suite is now about 1,700 tests.

Documentation was part of every task, not an afterthought. The guide has 16 chapters, with diagrams checked
by a parser in the test suite, a "scenario atlas" of every situation a task can meet with the test that
proves each one, and a chapter listing the mistakes.

## What the real runs taught me

Unit tests caught logic errors. **Every expensive surprise came from real containers and real runs.** A few
examples:

- **A sandbox escape through git.** While preparing a prompt-injection drill, I found that a malicious
  `.git` directory left behind by the model could make the *harness's* git commands run code outside the
  sandbox (hooks, filters, an `fsmonitor` setting). The drill itself was refused at every step by the
  model. But the real hole was in my own plumbing. The fix: the harness never trusts the agent's `.git`, and
  uses its own sealed copy with an allowlisted environment.
- **A symlink that leaked an environment.** A file the sandbox left in its output folder could be a symlink
  to somewhere it shouldn't point. Now files from a sandbox are read only through a function that refuses
  links and non-regular files.
- **Budgets that starved a reviewer.** Reserving money before a run stopped two runs spending the same
  dollars. But the first reviewer could reserve almost everything, leaving the second too little to
  finish. It was then retried with even less each time. Fixes: a minimum below which a run waits rather
  than starts, no retry once a run is out of budget, and later a fair share of what's left for each
  waiting review.
- **Reviews are most of the cost.** Across real tasks, about three quarters of the spend went to the two
  reviewers.
- **The human is the bottleneck.** On the final run, agents took about three minutes per task. Each pull
  request then waited 13 to 27 minutes for me to read and merge it.

I also compared reviewer models on the same sixteen past reviews. A cheaper model (Claude Sonnet 5.5) caught
the planted bug and agreed with every verdict, at about a seventh of the cost of the stronger one (Claude
Fable 5.1). But it missed about half of the smaller findings, mostly about whether tests really test
anything. The stronger model, run twice, found mostly the same things both times, but once changed its
verdict by rating the same issue more severely. One run of one model isn't a measurement.

**The numbers, for scale:** eight real tasks merged, about $1.40 per merged task all-in, 75% accepted on the
first review. The final five-task specification took 65 minutes from request to last merge, and cost $8.29.
The total API spend for building and proving all four phases was about $23.

## Limitations

This is where the "learning project" label earns its place:

- **The sample is tiny.** Eight tasks and one planted bug show that the loop works, not how well it works.
- **The main question is unanswered.** Does a team of agents beat one person driving one AI session? That
  needs hand-driven baselines on comparable work, and I haven't collected them yet.
- **One machine, one operator.** The API listens on localhost and isn't built for multiple users or for
  exposure to a network.
- **GitHub is still a way out.** A compromised agent can't reach my credentials. But its code does reach a
  branch, through the push the worker makes on its behalf. Reviews, CI and a human merge stand between that
  branch and `main`. On a free private repository, GitHub doesn't enforce that merge gate, so an early
  merge is possible (it's recorded and flagged).
- **One vendor.** Every agent runs on Claude models today. Cross-model review with a second runtime is still
  to come.
- **Small, well-scoped tasks only.** Everything so far has been changes of a few files. Larger or vaguer
  work is untested.

## What's next

The next phase is about scale and honesty:

- **A second runtime (Codex)**, so code written by one vendor's model is reviewed by another's.
- **More developers in parallel**, and delivering the test repository's whole roadmap end to end.
- **Continuous operation**: running unattended on real work for weeks, not demos.
- **Baselines**: recording how long comparable tasks take by hand, so the metrics can finally answer
  whether this is worth it.
- **Cheaper reviews where they're good enough**: for example, a cheaper model for security review and the
  stronger one for code review, measured rather than assumed.

## Closing

I set out to learn what an "AI engineering organization" actually needs. The models were the easy part.
The work was in everything around them: state machines that decide, reviewers that can't be talked into
approving, credentials an agent never sees, budgets that stop work before it's paid for, and an event log
that lets you check all of it afterwards.

If you build something similar, my one piece of advice is the pattern from the mistakes list: **run it for
real, early and small.** The tests told me the code was right. The real runs told me where it was wrong.

*Axiom is a personal learning project. Nothing here is a recommendation to run AI agents unattended on
code that matters.*
