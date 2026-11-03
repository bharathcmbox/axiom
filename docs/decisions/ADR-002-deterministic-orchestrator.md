# ADR-002: The orchestrator is deterministic code; LLMs only for judgment

Status: Accepted (2026-11-02)

## Context
The design has an "Engineering Manager" role. It could be an LLM that decides assignments and
next steps, or plain code.

## Decision
Scheduling, assignment, state transitions, retries, budgets and gates are deterministic code.
The Engineering Manager role uses an LLM only for judgment tasks: decomposing specs into tasks
(human-approved) and writing reports. Legal transitions are defined in one pure table per
state machine (`src/axiom/domain/`).

## Consequences
- Behaviour is testable, reproducible and cheap; failures are debuggable.
- Less "adaptive" scheduling; we can add LLM advice later behind the same command interface.
