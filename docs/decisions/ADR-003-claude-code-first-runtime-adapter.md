# ADR-003: Claude Code is the first worker runtime, behind an adapter

Status: Accepted (2026-11-02)

## Context
The original design proposed Codex first and Claude Code second. The project is being built
with Claude Code, and a second, different model is valuable mainly for independent review.

## Decision
Workers run through a runtime adapter interface (`start`, stream events, `cancel`, collect
result). The first implementation uses the Claude Agent SDK (Phase 2). Codex is added later,
primarily as a reviewer to give cross-model review. The `agents.runtime` column already allows
both.

## Consequences
- Orchestration code never imports a runtime SDK directly.
- Runtime-specific permissions and hooks live in the adapter.

## Revision (2026-11-13): Codex is no longer planned
Phase 5 was waiting on a Codex runtime for cross-model review, and there are no OpenAI keys. It now goes ahead on
Claude and the Anthropic platform only: review diversity comes from reviewers on models that differ from each
other and from developers (ADR-009), and the adapter is tested by a second Anthropic runtime, Managed Agents
(Phase 5, T6). A Codex runtime remains possible behind the same adapter; nothing depends on it.
