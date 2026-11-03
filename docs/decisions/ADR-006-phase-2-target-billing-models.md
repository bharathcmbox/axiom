# ADR-006: Phase 2 inputs: target repository, billing, models

Status: Accepted (2026-11-02)

## Context
Phase 2 runs the first real worker. Three inputs were open: which repository it works on, how
unattended runs are paid for, and which models the agents use.

## Decision
1. **Target repository: Agentic SOC.** It remains a separate repository, registered in Axiom as a
   project (ADR-004 still holds: no SOC concepts enter Axiom's code). The first Phase 2 task is a
   small, well-specified piece of it, so that the first end-to-end run tests Axiom rather than the
   size of the task.
2. **Billing: Anthropic API key (Console credits), not an interactive subscription.** The key is
   supplied only through the environment (`ANTHROPIC_API_KEY` in an untracked `.env` or a secret
   store); it never appears in prompts, task payloads, logs, the database or the repository.
   Recommended: a dedicated key in its own Console workspace with a spend limit, as a backstop to
   Axiom's own budget caps.
3. **Models: Claude only to start; Claude Opus 5.5 (`claude-opus-5-5`, $4 / $20 per MTok) for
   developer agents,** with effort set explicitly (its default is `medium`). Model choice is per
   agent (`agents.model`), so a cheaper model (e.g. Claude Sonnet 5.5, $2 / $10) can be trialled
   per role later, and kept only if cost per *accepted* task falls without quality dropping,
   measured from the event log. Reviewer model diversity (Codex, or a different Claude model) is
   decided in Phase 3.

## Consequences
- Budget enforcement (per execution, per task, daily) becomes a Phase 2 requirement, not an
  option, because real money is spent.
- Cost and token columns on `executions` start being filled.
