# ADR-016: Only the launcher holds the Docker socket

Status: Accepted (2026-11-06)

## Context
Workers started sandboxes themselves, so each mounted the Docker socket, which is in effect root on
the host. Any way into a worker was a way to own the machine. A socket proxy wouldn't help: anything
allowed to create containers can mount the host.

Designing this also exposed a credential path. The sandbox can write to `out/`, and the worker read
files from there. A sandbox could replace `sandbox.log` with a symlink to `/proc/self/environ` and
exit without an outcome, and the worker would copy its own environment, tokens included, into a
stored error. Confirmed with fake values. No sign it was used: no symlinks in 15 kept runs, and no
such text in stored errors. Errors weren't fed into the next prompt, so it couldn't reach an agent
that way.

## Decision
- **`sandbox-launcher` is the only service with the socket,** and the only image with the Docker
  CLI. Workers ask it over HTTP, with a token.
- **A request can only say what it has a field for:** a name, a run's `repo` and `out`, a few allowlisted
  settings, and optionally a shell command. Unknown fields are refused, so there's no way to ask for
  a mount, a privilege or an image. Every container gets the same restrictions.
- **Paths must be real directories in one run's folder under the workspaces root,** with no symlinks.
- **It can only kill containers it started.**
- **A worker that disappears takes its sandbox with it.** The worker holds the request open; when the
  connection drops, the launcher kills the container. This closes the orphan window ADR-015 left.
- **Nothing reads through a link.** `read_from_sandbox` is the only way to read what a sandbox wrote:
  it refuses symlinks and anything that isn't a plain file, and limits the size.
- **Logs don't live in the sandbox's folder.** The launcher captures them and returns them.
- **Errors are cleaned before they're stored:** anything shaped like a credential, and the exact values
  of this process's own secrets, are removed.

## Consequences
- A compromised worker no longer has root on the host. It can still start sandboxes on workspace
  paths, spending up to a budget, and it still holds its GitHub and agent tokens.
- The launcher is a valuable target. It runs no LLM or repository code and isn't reachable from a sandbox.
- If the launcher restarts, its connections drop and the workers' runs fail and retry. Its containers
  then need cleaning up: they keep running, without the model once their run token lapses.
