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
