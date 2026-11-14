# ADR-020: Managed Agents as a second runtime: go for reviews, as a drill; no-go for developers yet

Status: Accepted (2026-11-14). Phase 5 T6. Tests SPEC principle 2 (pluggable runtimes) and ADR-003.

## Context
Every agent ran through the Claude Agent SDK in Axiom's own sandbox, so "pluggable runtimes" had never been
tried. With Codex out of Phase 5, the second runtime is Claude Managed Agents: Anthropic runs the agent loop and a
per-session container. The question was whether it can keep the rules that matter: no credentials in the agent's
environment (ADR-008), read-only reviewers (ADR-009), a strict contract, a metered cost (ADR-015), and nothing
trusted from what the agent leaves behind (ADR-014).

## What was built
`runtimes/managed.py` implements the existing `ReviewRuntime` interface. **No orchestration code changed.**

| Rule | How the spike keeps it |
|---|---|
| No credentials in the agent's environment | The repository at the reviewed commit is uploaded as read-only files; no GitHub token is sent anywhere. The environment is `limited` with no hosts, no package managers and no MCP servers |
| Read-only | Only `read`, `glob` and `grep` are enabled; uploads are mounted read-only |
| A strict contract | The review is the input of a custom tool, `submit_review`, validated by `parse_review` with the role's checklist. The platform doesn't enforce a custom tool's schema, so an invalid submission goes back with its problems, up to 3 times |
| A spend cap | A platform-enforced session `budget` (the run budget) |
| A metered cost | The session's `usage.list_cost`, at list prices |
| Nothing trusted from the sandbox | Only the tool input is read, and it's validated |

## Result (live, [record](../runs/2026-11-14-phase-5-managed-agents.md))
Three reviews already run in the sandbox (T3) were run again on Managed Agents: the same verdicts and the same
blocking findings, at about the same cost ($0.71 against $0.81), about a minute each. One Sonnet review returned a
malformed contract once and passed when run again.

## Decision
- **Go for reviews, as a drill** (`drills/managed_review.py`). The adapter held without changes elsewhere, which is
  what principle 2 needed shown.
- **Not wired into the reviewer workers yet,** because two things conflict with existing decisions:
  1. **The Anthropic key.** The process that drives sessions needs it. ADR-015 says only the model proxy holds it.
     Wiring this in means moving the driver into the model proxy (or a new key-holding service), not giving the key
     to workers.
  2. **The record of cost.** The cost is the platform's list-price figure, not the model proxy's meter. That's a
     reasonable record, but a different one from every other run.
- **No-go for developers for now.** A developer needs to write and push. On Managed Agents that means a writable
  GitHub token given to Anthropic's git proxy, and Axiom's checks on the result (ADR-007, ADR-014) moved to read a
  branch the agent pushed. The self-hosted sandbox (private beta) would keep execution local and may change this.

## Consequences
- The runtime interface is proven, and Managed Agents is ready for an experiment on real reviews once the key's
  placement is decided.
- The whole repository at a commit is uploaded to the Files API for each review, and stays there until deleted.
  Fine for a public repository; a private one needs a decision on retention.
- Codex, if ever added, follows the same path: an adapter behind `ReviewRuntime`.
