# ADR-004: Axiom is product-agnostic; Agentic SOC is a customer project

Status: Accepted (2026-11-02)

## Context
An earlier spec mixed the Axiom platform with the Agentic SOC product it is meant to build
first. That blurs scope, schema and tests.

## Decision
This repository contains only the engineering organization. No security-product concepts appear
in its code, schema or docs beyond examples. Agentic SOC lives in its own repository and is
registered in Axiom as a project (Phase 5).

## Consequences
- Axiom can serve any project.
- Example task payloads in docs may mention alerts; code must not depend on them.
