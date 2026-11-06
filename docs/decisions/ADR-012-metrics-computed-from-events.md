# ADR-012: Metrics are computed from the event log, never stored

Status: Accepted (2026-11-06)

## Context
SPEC section 1 demands "a measured answer". Stored counters drift from reality and need migrations
whenever a definition changes.

## Decision
- `metrics.py` holds pure functions from task rows, events and costs to numbers. The only inputs are
  what the system already records. Changing a definition recomputes history.
- Facts only a human knows are recorded as **events**: `DEFECT_RECORDED` and `BASELINE_RECORDED`.
- Demo and planted work is tagged `demo` and excluded by default.
- The daily report and the dashboard read the same functions, and use no LLM.

## Consequences
- Each metric has one definition, unit-tested on scripted histories.
- Some numbers are upper bounds or proxies (your review time is elapsed time, not attention). Their
  caveats are stated wherever they are shown.
