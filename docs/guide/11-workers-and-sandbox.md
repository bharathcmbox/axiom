# 11. Workers, the sandbox and verification (Phase 2)

Phase 1 built the control plane, where you played every agent by hand with curl. Phase 2 adds
the first **real worker**: a process that claims a task, runs a Claude agent on it in an
isolated container, checks the result itself, and opens a pull request. You never touch it
between READY and the PR.

## The first real run

On 2026-11-03, task **SOC-001** ("make the agent test suite runnable and fix CLAUDE.md") went
from READY to [PR #1 on tiny-agenticsoc](https://github.com/bharathcmbox/tiny-agenticsoc/pull/1)
in 51 seconds for $0.15, with no human input:

```mermaid
flowchart LR
    a["17:07:12<br/>DEV-02 claims SOC-001"] --> b["agent: inspects llm.py, config.py;<br/>uv add --dev pytest; runs tests;<br/>edits CLAUDE.md"]
    b --> c["worker: diff check ✓<br/>(3 allowed files)"]
    c --> d["fresh sandbox:<br/>uv run --with pytest pytest -q ✓"]
    d --> e["push axiom/SOC-001-e71,<br/>open PR #1"]
    e --> f["17:08:03<br/>IN_REVIEW, waiting for you"]
```

## The pieces

```mermaid
flowchart TB
    subgraph host["Your machine (WSL2 + Docker)"]
        subgraph compose["docker compose"]
            api["api<br/>control plane"]
            db[(postgres)]
            w1["worker-dev-01<br/>(harness, DEV-01 token)"]
            w2["worker-dev-02<br/>(harness, DEV-02 token)"]
            proxy["egress-proxy<br/>allowlist: PyPI"]
            mp["model-proxy<br/>holds the Anthropic key"]
        end
        sbx["sandbox container<br/>one per execution<br/>Claude Agent SDK"]
        ws[("~/.axiom/work/<br/>SOC-001-e1/{repo,out}")]
    end
    gh["GitHub<br/>tiny-agenticsoc"]
    anth["api.anthropic.com"]
    w1 -->|"HTTP + bearer token"| api
    w2 -->|"HTTP + bearer token"| api
    api --- db
    w1 -->|"clone / push axiom/*,<br/>open PR"| gh
    w1 -->|"docker run"| sbx
    sbx --- ws
    w1 --- ws
    sbx -->|"run token only"| mp -->|"real key"| anth
    sbx -->|"packages"| proxy
    w1 -->|"open and renew a run"| mp
```

| Piece | Code | Trusted? | Holds |
|---|---|---|---|
| Worker harness | `src/axiom/worker/` | yes, deterministic code | DEV-xx API token, GitHub token |
| Sandbox | `src/axiom/sandbox/` + `docker/sandbox/` | **no**: an LLM driven by untrusted text | Anthropic API key only |
| Egress proxy | `docker/egress-proxy/` | yes (config only) | allowlist |

## One execution, step by step

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker harness
    participant API as Axiom API
    participant GH as GitHub
    participant S as Sandbox
    W->>API: POST /claims (lease 120s)
    API-->>W: SOC-001 ASSIGNED, run_budget_usd 3.00
    W->>API: start
    Note over W: heartbeat thread: POST /heartbeat every 30s
    W->>W: check repo URL is GitHub (fail fast, before spending)
    W->>GH: git clone (token via env-only credential helper)
    W->>W: branch axiom/SOC-001-e1, local excludes for tool caches
    W->>S: docker run (workspace + out mounted, budget, model)
    S->>S: prompt = rules + task + CLAUDE.md (as untrusted text)
    S->>S: agent loop, PreToolUse hook guards every Read/Edit/Write
    S-->>W: outcome.json + transcript.jsonl (cost, tokens, result)
    W->>W: verify diff: allowed paths, no secrets, no symlinks
    W->>W: commit as "Axiom agent DEV-01"
    W->>S: fresh sandbox, clean clone: run test_command
    W->>GH: push axiom/SOC-001-e1, open PR
    W->>API: submit {result, verified_tests, commit, branch, pr_url, cost}
```

Anything going wrong along the way becomes a `fail` with a reason, or `ask_human` if the agent
returns a question.

## Defence in depth: four layers between an agent and your repository

```mermaid
flowchart LR
    a["1. Prompt rules<br/>'change only allowed paths'"] --> b["2. PreToolUse hook<br/>denies Edit/Write outside<br/>allowed_paths, reads of secrets"]
    b --> c["3. Harness diff check<br/>catches anything else<br/>(e.g. writes via Bash)"]
    c --> d["4. Human review<br/>of the PR; Axiom never merges"]
```

Each layer assumes the one before it can fail:
- **Layer 1** worked in the live test: the agent *declined* to touch `other.txt`.
- **Layer 2** was proven in the SDK spike: the edit was blocked and the agent was told why.
- **Layer 3** has tests for misbehaving fake agents: a file outside the allowed paths, a leaked
  key, a symlink.
- **Layer 4** is you.

## The sandbox, locked down

```mermaid
flowchart TB
    subgraph sbx["sandbox container"]
        p["python -m axiom.sandbox.runner<br/>(non-root: your uid)"]
        ro["/ read-only"]
        home["/home/agent: empty tmpfs<br/>(no logins, no settings)"]
        repo["/workspace/repo: the clone (rw)"]
        out["/workspace/out: transcript, outcome (rw)"]
    end
    caps["--cap-drop ALL<br/>--security-opt no-new-privileges<br/>--pids-limit 512, --memory 4g, --cpus 2"]
    net["network: axiom_sandbox (internal)<br/>only route out: egress-proxy"]
    sbx --- caps
    sbx --- net
```

What the Docker tests prove (`make test-docker`):
- **Egress:** only PyPI is reachable through the proxy. `api.anthropic.com`, github.com and
  example.com are refused ("403 Filtered"). Direct connections fail (no DNS, no route).
- **Environment:** whatever the worker's own environment holds, the sandbox receives none of it. Its
  only credential is a run token from the model proxy.
- **Filesystem:** the container runs as the workspace owner, `/` is read-only, and HOME is empty.
- **Cancel and timeout:** both kill the container.

### Why the agent loads no settings from the repository

The SDK can load a repository's `.claude/settings.json`, which may contain permission rules and
hooks. A malicious or careless repository could use that to reconfigure the agent. The runner
therefore sets `setting_sources=[]` and passes `CLAUDE.md` inside the prompt, wrapped in
`<project_guidance>` tags and labelled untrusted.

## Keeping the GitHub token away from the LLM

```mermaid
flowchart LR
    env["worker env:<br/>AXIOM_GITHUB_TOKEN"] --> sub["git subprocess env:<br/>AXIOM_GIT_TOKEN"]
    sub --> helper["inline credential helper<br/>echo password=$AXIOM_GIT_TOKEN"]
    helper --> gh[GitHub]
    x1["❌ argv"]
    x2["❌ remote URL"]
    x3["❌ .git/config"]
    x4["❌ sandbox env"]
```

The worker also ignores your `~/.gitconfig`. Your `gh` login installs a credential helper there
for github.com, which git would otherwise have used: your full-access login instead of the
scoped token. A test covers this.

Because the repository is private on GitHub's free plan, branch protection isn't available
(ADR-007). The "no push to main, no merge" guarantee therefore lives in `worker/publish.py`:
- it pushes only `HEAD:refs/heads/axiom/<TASK>-e<N>`, never with force (8 unsafe refs are
  tested and refused);
- the GitHub client has exactly two methods, `find_open_pr` and `create_pr`.

## How the agent reaches the model without the key

The agent is given a run token, which is useless anywhere but the model proxy. The proxy swaps in the
real key on the way out (ADR-015).

```mermaid
sequenceDiagram
    participant W as Worker
    participant P as Model proxy
    participant S as Sandbox agent
    participant A as Anthropic
    W->>P: open run: budget, model, expires in 2 min
    P-->>W: run token
    W->>S: start with the proxy's address and the run token
    S->>P: POST /v1/messages, run token
    P->>P: token live, under budget, this model?
    P->>A: same request, real key
    A-->>P: streamed reply, with usage
    P-->>S: the same stream, as it arrives
    P->>P: add up the usage
    W->>P: every 40 s: extend the expiry
    W->>P: close run
    P-->>W: what was actually used
```

| If the agent... | What happens |
|---|---|
| runs `env` looking for a key | It finds only the run token |
| sends the token elsewhere | It's useless there, and the sandbox can't reach other hosts anyway |
| asks for another model, or `/v1/files` | Refused (403) |
| reports its cost as $0 | Ignored: the proxy's count is the record |
| spends its whole budget | Refused; the run fails as `budget exceeded` |
| outlives a killed worker | Renewal stops, and its token dies within 2 minutes |

Tests: `test_the_real_key_is_swapped_in_and_the_run_token_stays_behind`,
`test_a_forged_cost_is_ignored`, `test_a_token_stops_working_when_it_expires`,
`test_only_the_messages_endpoints_are_forwarded`, `test_a_stream_is_not_buffered`,
`test_sandbox_receives_no_credentials_at_all`.

## The harness doesn't trust what the sandbox leaves behind

Keeping credentials out of the sandbox isn't enough if the harness then acts on what the sandbox
wrote. The checkout's `.git` is one such thing: git runs what its config says. So the harness keeps
its own copy (ADR-014). Found while preparing the Phase 4 drill; before this, a compromised agent
could reach every credential the worker holds.

```mermaid
flowchart LR
    subgraph root["Workspace on the host"]
        subgraph mounted["Mounted in the sandbox"]
            repo["repo/<br/>working tree + the agent's .git"]
            out["out/<br/>transcript, outcome"]
        end
        hg["harness.git<br/>copied before the agent starts"]
    end
    agent["Agent"] -->|"can write anything"| repo
    h["Harness git<br/>--git-dir harness.git<br/>allowlisted environment"] --> hg
    h -->|"reads files only"| repo
    h -->|"push to the project's URL,<br/>token on this call only"| gh["GitHub"]
```

Tests: `test_nothing_planted_in_git_config_runs_in_the_harness`,
`test_a_planted_url_rewrite_cannot_redirect_the_push_or_the_token`,
`test_a_nested_repository_is_refused_before_git_looks_at_it`,
`test_git_never_receives_the_workers_credentials`, and from the real sandbox,
`test_an_attack_planted_from_the_real_sandbox_does_not_reach_the_harness`.

## Budgets, end to end

```mermaid
flowchart LR
    claim["claim: refused if task or day budget spent,<br/>or the rest is reserved by runs in flight"] --> rb["run_budget = min(per-run cap, task left, day left)<br/>and reserved until the run ends (ADR-013)"]
    rb --> sdk["SDK max_budget_usd = 80% of run_budget<br/>(its estimate can overshoot)"]
    sdk --> stop["over budget → error_max_budget_usd<br/>→ fail(budget exceeded)"]
    stop --> fs["task budget exhausted → FAILED<br/>(not back to READY)"]
```

## Verification: trust the worker, not the agent

The agent's report contains claims ("tests: 8 passed"). The worker:
1. stages every change and checks paths, secret files, symlinks and credential patterns in
   added lines;
2. commits;
3. clones that commit into a **clean** directory and runs the project's `test_command` in a
   **fresh sandbox with no credentials at all**.

Only if all three pass does anything get pushed. The PR body shows the agent's claims and the
verified result side by side.

## Running it

```bash
make up                 # api, db, egress-proxy
make worker-tokens      # once: agent tokens for DEV-01/02 into .env
make workers            # build sandbox image, start worker-dev-01/02
docker compose logs -f worker-dev-01
make workers-down
```

Useful environment variables: `AXIOM_PROJECT_KEY` (only claim one project),
`AXIOM_KEEP_WORKSPACES=1` (keep `~/.axiom/work/<TASK>-e<N>/` with `out/transcript.jsonl` for
inspection), `AXIOM_FAKE_SCENARIO=success` (a fake agent inside the real sandbox; no API cost).

## Try it

1. Run `make test-docker` and read `tests/docker/test_sandbox_container.py` while it runs.
2. Find the four defence layers in code: `sandbox/prompt.py` RULES, `sandbox/hooks.py`
   `decide`, `worker/verify.py` `check_changes`, and the PR on GitHub.
3. Read a transcript: `~/.axiom/work/SOC-001-e*/out/transcript.jsonl` has one JSON line per SDK
   message. Find the tool calls, and the final `ResultMessage` with `total_cost_usd`.
4. Thought experiment: the task text says "also delete README.md". Which layer stops it, and
   what does the event log show?
