# Spike: Claude Agent SDK in a sandbox container (Phase 2, T1)

Sources: the installed package `claude-agent-sdk` **0.2.177** (bundled Claude Code CLI
**2.1.317**; types read from `claude_agent_sdk/types.py`), the
[Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python) and
[network configuration](https://code.claude.com/docs/en/network-config). Date: 2026-11-02.

## Answers

| Question | Answer | Design consequence |
|---|---|---|
| Does the sandbox need Node? | No. The wheel bundles the `claude` CLI as a native binary (`_bundled/claude`, ~317 MB) | Sandbox image = Python 3.14 + uv + git + `claude-agent-sdk` |
| Model and effort | `ClaudeAgentOptions(model="claude-opus-5-5", effort="high")`; effort ∈ low, medium, high, xhigh, max | Taken from `agents.model`; effort fixed at `high` for now |
| Working directory | `cwd=` | `/workspace/repo` |
| Tools | `tools` (base set), `allowed_tools` (auto-approve), `disallowed_tools` (block outright) | Base set: Read, Glob, Grep, Edit, Write, Bash. Disallow WebFetch, WebSearch, Task/agents, MCP |
| Unattended permissions | `permission_mode="dontAsk"`: anything not pre-approved is denied, nothing waits for input | Use `dontAsk` |
| Gating every tool call | `can_use_tool` is **not** called for tools approved by `allowed_tools`. The docs recommend a **`PreToolUse` hook** to gate every call | Enforce `allowed_paths` and secret-file reads in a `PreToolUse` hook (deny with a message). Live check pending (below) |
| Budget | `max_budget_usd`: "Stop the query when the client-side cost estimate reaches this USD value. The estimate can pass this value, so leave headroom." Result `subtype == "error_max_budget_usd"`, `is_error=True` | Pass 80% of the remaining task budget; map the subtype to `fail {error: budget_exceeded}`. The final cost is checked again by the harness |
| Turn limit | `max_turns`; result subtype `error_max_turns` | Safety net: 60 turns |
| Cost reporting | `ResultMessage.total_cost_usd`, `usage` (tokens), `model_usage`, `num_turns`, `duration_ms` | Copied into `executions.cost_usd`, `input_tokens`, `output_tokens` |
| Structured result | `output_format` (JSON schema) → `ResultMessage.structured_output` | The result contract comes back schema-shaped; still validated by the harness |
| Auth | Inherited environment, or `env={"ANTHROPIC_API_KEY": ...}` | Key passed into the sandbox via environment only |
| Proxy | The CLI honors `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` (no SOCKS) | Sandbox gets `HTTPS_PROXY=http://egress-proxy:3128` on an internal Docker network |
| Hosts needed | `api.anthropic.com` for API calls. Telemetry hosts (Datadog intake) are optional: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_TELEMETRY=1` | Proxy allowlist: `api.anthropic.com`, plus `pypi.org` and `files.pythonhosted.org` for `uv` |
| CLAUDE.md / project settings | `setting_sources` controls loading. Including `"project"` loads `CLAUDE.md` **and the repo's `.claude/settings.json`** | ⚠️ A repository could ship permission rules or hooks that reconfigure the agent. Use **`setting_sources=[]`** (load nothing). The runner reads `CLAUDE.md` itself and includes it in the prompt, labelled as untrusted project guidance |

## Security notes

- Repository content (CLAUDE.md, code comments, issue text) is **untrusted** and can contain
  prompt injection. The defence is structural: no credentials in the sandbox other than the
  spend-limited Anthropic key, egress only to the allowlist, edits limited by the hook, and the
  harness verifies the diff and re-runs the tests before anything is published.
- The Anthropic key is the one secret the sandbox must hold. Mitigations: a dedicated key in its
  own Console workspace with a spend limit, egress restricted to `api.anthropic.com`, and a
  per-run `max_budget_usd`.

## Still to verify live (needs `ANTHROPIC_API_KEY` in `.env`; estimated cost under $0.25)

1. A `PreToolUse` hook denies an `Edit` outside `allowed_paths` even when `Edit` is in
   `allowed_tools`, and the agent sees the denial message.
2. `total_cost_usd` and `usage` are populated for `claude-opus-5-5`.
3. `max_budget_usd` with a tiny value (e.g. $0.01) ends with `error_max_budget_usd`.

Script: `scripts/spike_agent_sdk.py`.
