# ADR-014: The harness never trusts the agent's `.git`

Status: Accepted (2026-11-06)

## Context
ADR-008 keeps credentials out of the sandbox. But the sandbox can write anywhere in its checkout,
including `.git`, and the harness then ran git in that checkout with its whole environment. Git
executes what its config says, so the sandbox boundary could be crossed without touching the
sandbox:

| Planted by the agent | Effect in the harness, before this ADR |
|---|---|
| `core.fsmonitor` in `.git/config` | ran code, with the worker's environment |
| `filter.*.clean` plus attributes | ran code, with the worker's environment |
| `url.*.insteadOf` | sent the push, and the GitHub token, to another host |
| a nested repository with its own config | ran code during `git status` |
| hooks | already blocked by `core.hooksPath` |

The worker holds the GitHub token, the Anthropic key and the agent's Axiom token, mounts the Docker
socket (in effect root on the host), and isn't behind the egress proxy. Found while preparing the
Phase 4 prompt-injection drill. No sign it had been used.

## Decision
1. **Seal.** Before an agent can touch a checkout, the harness copies `.git` to `harness.git` beside
   `repo/` and `out/`, the only directories mounted in the sandbox. Afterwards every git call the
   harness makes on that checkout uses `--git-dir harness.git --work-tree repo`. The agent's `.git`
   is never read.
2. **Least environment.** A git subprocess gets an allowlist of variables, never the worker's whole
   environment. The GitHub token is added only to the calls that need it.
3. **Explicit push target.** Pushes name the project's URL, not a remote.
4. **Nested repositories are refused** before git looks at the tree.

## Consequences
- An agent's own `git commit` in the sandbox doesn't count: only the working tree does. The rules
  already forbade it.
- `check_changes` walks the tree, which costs little.
- This closes the path through git. The worker should still be treated as high value: the harness
  reads other files the sandbox wrote (`outcome.json`, the transcript), and it still holds the Docker
  socket. Those are listed in chapter 13.
