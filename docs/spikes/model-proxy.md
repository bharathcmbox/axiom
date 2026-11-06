# Spike: running the agent through a model proxy (Phase 4 H0, 2026-11-06)

**Question:** can the bundled CLI and SDK run a real agent with the key held outside the sandbox?
**Answer: yes.** See the [hardening plan](../plan/phase-4-hardening.md). Live cost about $0.06.

A throwaway proxy, on the sandbox network and the outside network, held the real key. The real image
and runner ran in the sandbox with `ANTHROPIC_BASE_URL` pointing at the proxy,
`ANTHROPIC_API_KEY` set to a fake run token, and the proxy's name in `NO_PROXY`.

| Test | Result |
|---|---|
| Real run, Haiku 5.5: tools, streaming, structured output | Completed. Input 18,626 and output 466 tokens, matching the SDK exactly |
| Real run, Opus 5.5 (production model) | Completed. Metered $0.053089 at the price table; the SDK said $0.0530888 |
| Refuse the second request with 403 | Failed in 4 s, without retrying |
| Proxy not running | Failed after 181 s, without hanging |
| No token, or a wrong one | 401 |
| Good token on `/v1/models`, `/v1/organizations/me`, `/v1/files` | 403: only the Messages endpoints are forwarded |
| Real key in the spike's workspaces | Not found |

## What H1 must handle

- **Authentication header:** the CLI sent `x-api-key`, and made only `/v1/messages` calls here. With
  non-essential traffic off, nothing else was attempted.
- **A refused run reports badly.** The outcome's error read `success: success: `, because the runner
  repeats the result's subtype. A budget refusal must say so.
- **The SDK's cost can't be trusted on a refused run:** it reported $0.00016 for a request whose tokens
  compute to about $0.0012. The cause wasn't investigated. Metered cost must be the record.
- **Failure takes 181 s when the proxy is down,** so a run lasts that long before failing. Acceptable.

## Not tested

Long streams, parallel runs, a Fable review, and a reply cut off mid-stream. H1 covers these.
