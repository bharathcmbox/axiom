# ADR-015: The model proxy holds the Anthropic key

Status: Accepted (2026-11-06)

## Context
ADR-008 gave each sandbox the real Anthropic key, because the agent needs the model. So any agent
tricked into running `env` could read it, and with `api.anthropic.com` allowlisted, data could leave
under someone else's key. The sandbox also wrote its own cost report, and a sandbox orphaned by a
killed worker kept spending. See the [hardening plan](../plan/phase-4-hardening.md) and the
[H0 spike](../spikes/model-proxy.md).

## Decision
A small service, `model-proxy`, is the only holder of the key. It runs no LLM and no repository code.

- **Run tokens.** For each run, the worker opens a run with a budget, a model and an expiry, and
  passes the sandbox a token. It works only at the proxy, for that model, under that budget.
- **Only the Messages endpoints** are forwarded, only to Anthropic. The proxy logs no headers or bodies.
- **Expiry and renewal.** The worker renews while the sandbox runs. If the worker dies, renewal stops
  and the sandbox loses the model within one expiry (120 s). A lapsed run can't be revived.
- **Metered cost is the record.** The proxy counts Anthropic's own usage figures at the price table,
  and that replaces whatever the sandbox wrote. A killed worker's run is read back on restart, using
  a file kept outside the sandbox's mounts. A transcript estimate is only the fallback.
- **No fallback to a direct key.** If the proxy is down, runs fail. `api.anthropic.com` is off the
  sandbox's allowlist, and the sandbox and the worker never receive the key.
- **State is in memory.** If the proxy restarts, tokens die and runs fail and retry. Its admin token
  lets a worker open runs, so it's held only by workers and the proxy.

## Consequences
- A compromised agent has nothing to steal. The most it can do with its token is use its own budget.
- A compromised worker has no Anthropic key, though its admin token can still open budgeted runs.
- Budgets now hold against a dishonest sandbox.
- A request in flight can overshoot a run's budget by its own cost, since cost is known only afterwards.
- The price table must stay current, or metering drifts. `test_prices_reproduce_a_real_billed_run`
  checks it against a real bill, and live runs have matched the SDK exactly.
- One more service to run, and it's a valuable target. It's small, standard-library only, and accepts
  one kind of request.
