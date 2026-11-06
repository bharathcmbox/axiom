# Axiom — specification

*Intelligence, orchestrated.*

Distilled from the original design conversation (2026-11-02) plus the decisions in
[docs/plan/phase-1.md](docs/plan/phase-1.md).

## 1. What Axiom is

Axiom is an **AI engineering organization**: a control plane that runs a team of coding agents
on real software work. You act as CTO (direction, specifications, priorities, approvals); Axiom
breaks work into tasks, assigns agents, tracks progress, gates quality, and reports.

Coding harnesses (Claude Code, Codex) are the **workforce**. Axiom is the **organization** around
them. It does not reimplement the agent loop.

```
You (CTO) ─ direction · specifications · priorities · approvals
   │
Axiom control plane ─ backlog · task/execution state machines · scheduling · leases
   │                  · agent registry · budgets · event log · reports
Worker runtimes ─ Claude Code (Claude Agent SDK) first · Codex second (mainly review)
   │
Shared infrastructure ─ GitHub (branches, PRs, Issues mirror) · GitHub Actions · containers/worktrees
```

Axiom is **product-agnostic**. It contains no knowledge of any specific product. Its first
customer project (Agentic SOC) lives in its own repository and is registered as a project.

## 2. Goal and measure of success

Give Axiom a product specification. It breaks the work into tasks, assigns agents, implements,
reviews, tests, opens PRs, and reports, with a human approving merges.

Success is not the number of agents. It is the quality and economics of delivery:

- specification → PR / merged cycle time
- first-pass review acceptance rate and rework loops per task
- security findings per PR
- cost per completed task (not per agent)
- human interventions per task
- defects found after merge
- whether parallel execution reduces delivery time without reducing quality

All metrics are computed from the event log.

## 3. Principles (non-negotiable)

1. Axiom is the orchestration platform, not another LLM or coding agent. Scheduling,
   transitions, retries, budgets and gates are deterministic code. LLMs are used only for
   judgment: decomposing specs, implementing, reviewing, summarizing.
2. Agents run through pluggable runtime adapters (`start`, stream events, `cancel`, collect result).
3. One task execution = one isolated workspace (own git worktree + branch, in a container).
4. Everything is persisted: tasks, executions, reviews, events. Axiom recovers from restarts
   without relying on any agent's conversation history.
5. Agents exchange structured contracts (JSON with a schema), not chat.
6. Nobody approves their own work. A developer never marks its own task done; independent
   review, CI, and a human gate the merge.
7. No autonomous merging or production deployment.
8. Budgets are enforced, not just reported (per-execution, per-task, daily).
9. The backlog is untrusted input. Workers get least-privilege tools and scoped tokens.
10. Prefer simple, maintainable infrastructure: one VM, Docker Compose, PostgreSQL. No
    Kubernetes, Kafka, Redis or vector DB.

## 4. Roles (initial organization: 7 logical agents, 2 concurrent developers)

| Role | Count | Responsibility |
|---|---|---|
| Engineering Manager | 1 | Decomposes specs, tracks progress, reports. Assignment and next-step decisions are deterministic code; the LLM is used for decomposition and reports. |
| Architect | 1 | Component boundaries, interfaces, design review |
| Developer | 2 | Implements tasks in its own worktree; opens PRs |
| QA Engineer | 1 | Tests independently; does not edit implementation files |
| Security Reviewer | 1 | Read-only security review |
| Code Reviewer | 1 | Read-only correctness and maintainability review |

Roles are logical; several agents can share a runtime and model. Merging is a workflow step
(review PASS + CI green + human approval), not an agent.

## 5. Delivery workflow

Spec submitted → decomposition into tasks with dependencies and acceptance criteria (human
approves) → tasks become READY when dependencies are done → developer claims a task →
implements in an isolated workspace → submits result + PR → independent review (QA,
security, code) → changes requested loops back → CI → human merge approval → done →
dependents unblock → reports.

State machines: [docs/state-machines.md](docs/state-machines.md).

## 6. Contracts

**Task** (written by the planner, approved by the human):

```json
{
  "task_id": "AX-001",
  "title": "Implement alert ingestion API",
  "assigned_role": "developer",
  "priority": "P1",
  "specification": "Implement POST /alerts",
  "acceptance_criteria": ["Validate incoming alert schema", "Reject malformed payloads"],
  "dependencies": [],
  "max_retries": 2,
  "budget_usd": 3.00
}
```

**Result** (returned by a developer): what was implemented, files changed, tests run, each
acceptance criterion with evidence, known limitations, PR URL, cost.

**Review** (returned by a reviewer): `PASS`/`FAIL`, findings with severity, file/line, required
change.

Test results in a result contract are claims. Axiom trusts CI, not the agent.

## 7. Data model (PostgreSQL, source of truth)

- `projects`: specification, repo, settings
- `agents`: registry of role, runtime, model, instructions, concurrency, enabled
- `tasks` + `task_dependencies`: contract fields, state, retry and review counters
- `executions`: one row per agent run: lease, status, result, cost, tokens, transcript
- `reviews`: review contracts per role and round, with their cost
- `events`: append-only log of everything that happens

GitHub PRs and CI feed back through the sync. An Issues mirror is planned for Phase 4; GitHub is
not the source of truth.

## 8. Guardrails

- Per-role permissions enforced in the runtime adapter; reviewers read-only.
- Claude Code hooks in workers: block edits outside allowed paths, block secrets in diffs,
  restrict network egress.
- Scoped GitHub token per worker: create branches and PRs, cannot merge or change settings.
- Branch protection on main: required CI, required human review, no force push.
- Injection drill: seed an issue with a prompt-injection payload; verify nothing escalates.
- Unattended workers use API billing, not interactive subscriptions. Check terms first.

## 9. Infrastructure and budget

Local machine for development and administration; one cloud VM (Docker Compose) for the
control plane and workers. Initial limits: 2 concurrent developers, 2 retries per task,
configurable per-task and daily budgets, no automatic merge or deployment. Target budget
around $200/month for the prototype.

## 10. Phases (each ends with a working demo)

Status: Phases 1, 2 and 3 are complete; see [docs/plan/](docs/plan/).

1. **Control plane skeleton.** Schema, agent registry, task + execution state machines, leases,
   event log, REST API, Docker Compose, tests. *Exit:* drive tasks through every state by hand;
   kill and restart; state intact.
2. **First worker.** Claude Code runtime adapter, worktree + container per execution, result
   contract, PR creation, GitHub integration (Issues mirror), budget cap. Target: a small,
   well-scoped task in the Agentic SOC repository (ADR-006). *Exit:* one real task goes from
   READY to an open PR with no human input.
3. **Quality organization.** Code and security reviewers on a different model; review contracts;
   changes-requested loop on the same PR; CI results; human merge on GitHub, observed by a sync.
   *Exit:* a deliberately buggy task is caught and sent back.
4. **Engineering management.** Spec decomposition with human approval, QA agent, Issues mirror,
   cost accounting and metrics, dashboard (Streamlit) and daily report. *Exit:* a multi-task spec
   delivered end to end.
5. **Scale and diversify.** Codex runtime (cross-model review), more developers, Agentic SOC
   delivered end to end, continuous operation.

## 11. Out of scope for now

Automatic merge, deployment, production monitoring, more than two concurrent developers,
multi-repo work, fancy UI.
