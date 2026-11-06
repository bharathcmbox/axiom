# ADR-007: Enforce "agents only open PRs" without GitHub branch protection

Status: Accepted (2026-11-02)

## Context
SPEC principle 9 wants "agents only open PRs" enforced by branch protection, not policy. The
target repository is private on GitHub's free plan, where branch protection and rulesets are
unavailable (HTTP 403). A token that can push branches can therefore also push to `main` and
merge pull requests.

## Decision
- The GitHub token (fine-grained, one repository, Contents + Pull requests read/write, nothing
  else) is held **only by the worker harness**: deterministic code that never executes model
  output.
- The LLM runs in a separate sandbox container with **no GitHub credentials** and no network
  route except an allowlist proxy.
- The harness pushes only refs matching `refs/heads/axiom/*`, opens pull requests and has no
  code path that merges. A test asserts the ref guard.
- Upgrading to GitHub Pro, or moving to a GitHub App or machine user, would make GitHub enforce
  protection on `main` as well. Deferred by ADR-010; revisit before relying on Axiom for real work.

## Consequences
- The guarantee rests on the trust boundary between harness and sandbox, so that boundary must
  be tested (no credentials in the sandbox environment or workspace).
- PRs are authored as the token owner (the human). GitHub will not let them approve their own
  PRs; acceptable while merges are manual and protection is absent.
