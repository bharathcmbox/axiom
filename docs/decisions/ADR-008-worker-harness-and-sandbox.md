# ADR-008: Worker = deterministic harness + one sandbox container per execution

Status: Accepted (2026-11-02)

## Context
A worker must claim tasks, keep a lease alive, prepare a workspace, run a Claude agent, verify
the outcome and publish a PR. Task text and repository files are untrusted input (prompt
injection). The agent loop needs an Anthropic API key; GitHub and Axiom credentials must stay
out of its reach.

## Decision
- **Worker harness:** a long-running compose service (`worker`), our own deterministic code. It
  talks to the Axiom API with its agent token, holds the GitHub token, and starts sandbox
  containers through the Docker socket.
- **Sandbox:** a fresh container per execution, built from `axiom-sandbox`. It mounts only the
  execution's workspace (a fresh clone with credentials removed) and an output directory, gets
  the Anthropic key and budget, and reaches the network only through an allowlist egress proxy.
  It runs the Claude Agent SDK with tool restrictions and hooks (no edits outside
  `allowed_paths`, no reads of secret-looking files).
- **Verification before publishing:** the harness validates the result contract, checks that
  changed paths are within `allowed_paths`, scans the diff for secrets, and re-runs the project's
  test command in a second fresh sandbox. Only then does it push and open a PR.
- The runtime is behind an adapter (`runtimes/base.py`), with a FakeRuntime for tests, so almost
  all tests run without API calls.

## Consequences
- The Docker socket gives the harness host-level power; acceptable because the harness never
  executes model output, but the harness code must stay small and reviewed.
- Each execution pays container start-up time (seconds), which is negligible next to agent runtime.
