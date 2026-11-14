# ADR-010: Humans merge on GitHub; Axiom observes; no GitHub Pro yet

Status: Accepted (2026-11-03). Supersedes the "upgrade before Phase 3" note in ADR-007.

## Context
Phase 3 needs CI results and human merge approval. On a private repository on GitHub's free
plan, rulesets and branch protection are unavailable. A GitHub App would let a bot author PRs
(so the owner can approve them), but approvals are only enforced with a ruleset.

## Decision
- **The human merges on GitHub.** A deterministic **sync service** polls PRs of tasks in
  IN_REVIEW / AWAITING_MERGE and maps GitHub to commands: merged → `merge` (recorded as
  `human:<github login>`), closed without merge → `cancel`, a "changes requested" review →
  `reject_merge` / `request_changes`, CI conclusion → input to the aggregator.
- **Axiom still has no merge code path.** The sync only reads GitHub.
- **No GitHub Pro and no GitHub App for now.** The existing fine-grained token is kept and
  additionally granted **Actions: read** to read CI results.
- CI runs on GitHub Actions in the target repository (`.github/workflows/ci.yml`), added by a
  human-approved commit. The worker token has no Workflows permission, so agents cannot
  change CI.

## Consequences
- GitHub does not *enforce* "PR required" or "CI green before merge"; the human can merge
  early or push to `main`. Axiom shows the gate state on the task; enforcement is convention.
  Revisit (Pro + ruleset, possibly a GitHub App) before relying on Axiom for real work.
- PRs keep showing the token owner as author.

## Revision (2026-11-07): only trusted people can send work back
Checking the repositories before making them public found that a "Request changes" review was accepted from
anyone, while `/axiom changes` comments were already limited to owners, members and collaborators. On a public
repository that would let any GitHub user send an agent's PR back with their own text as feedback, which reaches
the developer agent's prompt. Reviews now pass the same `author_association` check as comments.

## Revision (2026-11-13): the sync may write to one issue
ADR-019 lets the sync post notifications as comments on a pinned `Axiom inbox` issue in the project repository,
with text chosen by code only, and needs Issues: write on the sync token for that. It writes nothing else, it
never reads replies on that issue, and it still has no merge code path. GitHub enforcement (Pro or an App) is
still deferred: the public tiny-agenticsoc repository has a ruleset requiring a PR and the `tests` check
(Phase 5 Q9).
