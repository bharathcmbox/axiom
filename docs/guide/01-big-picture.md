# 1. The big picture

## The problem Axiom solves

When you use Claude Code today, you drive **one** agent through **one** session. It plans,
edits, runs tests and gives you code. People who say they "manage 40 agents" are doing
something different. They run an **organization**: a backlog, people with roles, hand-offs,
reviews, gates and reports, where the workers happen to be coding agents.

The coding agents can be identical in both cases. What differs is the machinery around them.
**Axiom is that machinery.**

```mermaid
flowchart LR
    subgraph today["Today: one session"]
        y1["You"] --> cc["Claude Code session"] --> code1["Code"]
    end
    subgraph axiom["Axiom: an organization"]
        y2["You (CTO)"] --> ax["Axiom control plane"]
        ax --> d1["Developer agents"]
        ax --> r1["Reviewer agents"]
        ax --> q1["QA agent"]
        d1 --> pr["Pull requests"]
        r1 --> pr
        q1 --> pr
        pr --> gate{"Review PASS +<br/>CI green +<br/>your approval"}
        gate --> main["main branch"]
    end
```

## The four layers

```mermaid
flowchart TB
    L1["<b>You</b><br/>direction · specifications · priorities · approvals"]
    L2["<b>Axiom control plane</b>  ← Phase 1 builds this<br/>backlog · state machines · leases · agent registry · event log · REST API"]
    L3["<b>Worker runtimes</b>  ← Phase 2+<br/>Claude Code via the Claude Agent SDK · Codex later"]
    L4["<b>Shared infrastructure</b>  ← Phase 2+<br/>GitHub branches, PRs, Issues mirror · GitHub Actions · containers + worktrees"]
    L1 --> L2 --> L3 --> L4
```

Two rules shape everything else (see [ADR-002](../decisions/ADR-002-deterministic-orchestrator.md)):

1. **The control plane is ordinary deterministic code, not an LLM.** Who may do what, which
   task goes next, when to retry and when to give up are all plain Python and SQL. LLMs are
   used only where judgment is needed: writing code, reviewing, decomposing specs.
2. **All state is in PostgreSQL** ([ADR-001](../decisions/ADR-001-postgres-source-of-truth.md)).
   No agent's chat history, and nothing in process memory, is needed to know where things stand.

## The organization (roles)

```mermaid
flowchart TB
    cto["You: CTO"]
    em["EM-01<br/>Engineering Manager"]
    arch["ARCH-01<br/>Architect"]
    dev1["DEV-01<br/>Developer"]
    dev2["DEV-02<br/>Developer"]
    qa["QA-01<br/>QA Engineer"]
    sec["SEC-01<br/>Security Reviewer"]
    rev["REV-01<br/>Code Reviewer"]
    cto --> em
    em --> arch
    em --> dev1
    em --> dev2
    em --> qa
    em --> sec
    em --> rev
```

These seven are registered from [config/agents.toml](../../config/agents.toml) at startup. In
Phase 1 they are only rows in the `agents` table. You "become" one by using that agent's API
token (`python -m axiom.registry.tokens issue agent:DEV-01`). From Phase 2, a worker process
holds the token and does that for real.

## Phases

```mermaid
flowchart LR
    p1["<b>Phase 1</b><br/>Control plane skeleton<br/>✅ done"]
    p2["<b>Phase 2</b><br/>First worker<br/>Claude Code adapter,<br/>worktree + container, PR"]
    p3["<b>Phase 3</b><br/>Quality organization<br/>reviewers, CI gate,<br/>changes-requested loop"]
    p4["<b>Phase 4</b><br/>Engineering management<br/>spec decomposition,<br/>dashboard, reports"]
    p5["<b>Phase 5</b><br/>Scale + diversify<br/>Codex review,<br/>continuous operation"]
    p1 --> p2 --> p3 --> p4 --> p5
```

## What Phase 1 actually contains

```mermaid
mindmap
  root((Phase 1))
    Data
      projects
      agents registry
      tasks + dependencies
      executions with leases
      append-only events
    Rules
      task state machine
      execution state machine
      actor permissions
      guards
    Mechanics
      command engine
      SKIP LOCKED claims
      concurrency caps
      heartbeats
      reaper
    Interfaces
      REST API + OpenAPI
      docker compose
    Proof
      634 tests
      invariant checker
      SIGKILL restart demo
      clean-clone smoke test
```

What Phase 1 deliberately did **not** contain: any LLM call, any worker, or any GitHub access.
Those arrive in Phase 2. (Bearer-token authentication was added at the start of Phase 2.)

## Try it

1. `docker compose up -d --build --wait`, then open <http://127.0.0.1:8000/docs>. Every
   endpoint in the guide can be tried from that page.
2. Issue yourself a token (`docker compose exec api python -m axiom.registry.tokens issue human:me`),
   then `curl -s -H "Authorization: Bearer <token>" localhost:8000/agents | python3 -m json.tool`
   lists your seven employees.
3. Read [SPEC.md](../../SPEC.md) sections 1–4. Then find each of the two rules above in the
   code: search for `check(` in `orchestrator/commands.py`, and look for any in-memory state in
   `api/app.py` (there is none apart from the connection pool).
