# The Axiom guide: how it is built and how it works

This guide is for learning. It explains what Axiom is (Phases 1–3 are built), why it is built that
way, and how each piece works, with a diagram for almost every idea. It assumes you know Python and SQL,
but not Postgres locking or state-machine design.

Every chapter ends with **"Try it"** exercises against the running stack or the test suite.
Doing them is the fastest way to make the ideas stick.

## Reading order

| # | Chapter | You will learn |
|---|---|---|
| 1 | [The big picture](01-big-picture.md) | What Axiom is, its layers, what Phase 1 covers |
| 2 | [The life of a task](02-life-of-a-task.md) | One task from creation to DONE, call by call, with the events it leaves behind |
| 3 | [State machines](03-state-machines.md) | Why there are two machines, the loops (retry, rework, human input), who may do what |
| 4 | [The data model](04-data-model.md) | Every table, every constraint, and what the database guarantees for us |
| 5 | [The command engine](05-command-engine.md) | The single code path that changes state, step by step |
| 6 | [Concurrency, leases and the reaper](06-concurrency-and-leases.md) | How two agents never get the same task, and how dead workers are detected |
| 7 | [Crash recovery](07-crash-recovery.md) | Why killing Axiom loses nothing, and what happens at startup |
| 8 | [The API layer](08-api-layer.md) | How an HTTP request becomes a command, and how errors map to status codes |
| 9 | [How it is tested](09-testing.md) | Tests generated from the transition table, invariants, race tests |
| 10 | [Code tour and how to extend it](10-code-tour.md) | Where everything lives, and a worked example of adding a feature |
| 11 | [Workers, the sandbox and verification](11-workers-and-sandbox.md) | Phase 2: how a real agent runs safely, end to end, up to a pull request |
| 12 | [Independent review and the CI gate](12-reviews-and-ci.md) | Phase 3: reviewers on a different model, the aggregator, CI, merging on GitHub, the real run |
| 13 | [Scenario atlas](13-scenarios.md) | **Every situation a task can meet, each with a diagram and its proof** |
| 14 | [How it was built](14-how-it-was-built.md) | **The process, the order, the decisions, the mistakes, the cost** |
| 15 | [Metrics](15-metrics.md) | Phase 4: what is measured, from where, and how far to trust it |

**Short on time?** Read 1, then 13, then 14. Come back to the others when you want to know how a
piece works.

The reference documents sit next to this guide:
- [state-machines.md](../state-machines.md): the transition tables;
- [architecture.md](../architecture.md): a one-page summary;
- [api.md](../api.md): endpoints;
- [decisions/](../decisions/): the ADRs, i.e. why each choice was made.

## The whole system on one page

```mermaid
flowchart TB
    you(["You (CTO)"])
    subgraph axiom["Axiom control plane"]
        api["REST API + token auth"]
        engine["command engine<br/>(the only writer of state)"]
        rules["state machines<br/>(pure tables)"]
        agg["aggregator<br/>(reviews + CI → decision)"]
        reaper["reaper<br/>(expired leases)"]
        sync["GitHub sync<br/>(read-only)"]
        db[("PostgreSQL<br/>tasks · executions · reviews · events")]
    end
    subgraph workers["Workers: deterministic harness code, hold the tokens"]
        dev["developers<br/>DEV-01, DEV-02"]
        rev["reviewers<br/>REV-01, SEC-01"]
    end
    subgraph sandbox["Sandboxes: one per run, untrusted"]
        s1["Claude Opus 5.5<br/>edits files"]
        s2["Claude Fable 5.1<br/>read-only"]
    end
    gh["GitHub: branches, PRs, CI"]
    anth["Anthropic API"]
    you -->|"merge, /axiom changes"| gh
    you -->|"create tasks, answer, retry"| api
    dev --> api
    rev --> api
    api --> engine
    reaper --> engine
    agg --> engine
    sync --> agg
    sync --> engine
    engine -.->|"is this legal?"| rules
    engine --> db
    dev -->|docker run| s1
    rev -->|docker run| s2
    s1 -->|"via allowlist proxy"| anth
    s2 -->|"via allowlist proxy"| anth
    dev -->|"push axiom/*, open PR"| gh
    sync -->|reads| gh
```

## Glossary

| Term | Meaning |
|---|---|
| **Task** | A unit of work with a contract: title, specification, acceptance criteria, role, priority, retry limit, dependencies |
| **Execution** | One attempt by one agent to work on a task. A task can have many over its life, but only one active at a time |
| **Agent** | A logical employee in the registry (e.g. `DEV-01`, a developer running on Claude Code). A worker process acts as an agent using its API token |
| **Actor** | Whoever issues a command: `human:<name>`, `agent:<KEY>` or `system:<name>`. Over HTTP it comes from the bearer token |
| **Worker** | Deterministic harness code that runs one agent's work: claims, heartbeats, verifies, publishes. It holds the tokens; the LLM never does |
| **Sandbox** | A short-lived locked-down container where the LLM runs, with no GitHub or Axiom credentials |
| **Review** | One reviewer role's verdict on one round of a task's work, returned as a strict contract |
| **Aggregator** | Plain code that turns reviews and CI into one decision: approve, request changes, escalate or wait |
| **Round** | One submission and its reviews. Rework starts the next round, on the same branch and PR |
| **Command** | A request to change a task's state, e.g. `start`, `submit`, `merge`. Legal or illegal depending on the current state and the actor |
| **Event** | A row in the append-only log, written in the same transaction as every state change |
| **Lease** | A time-limited claim on a task held by an execution. Heartbeats renew it; if it expires, the reaper takes the task back |
| **Reaper** | A background loop that times out executions whose lease expired |
| **Invariant** | A rule that must always hold, e.g. "a task is ASSIGNED or IN_PROGRESS iff it has exactly one active execution" |
| **Guard** | A condition checked before a legal command is applied, e.g. "the lease is still live" |
