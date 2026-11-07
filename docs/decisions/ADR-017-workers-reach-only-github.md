# ADR-017: Workers and GitHub sync reach only GitHub

Status: Accepted (2026-11-06)

## Context
Sandboxes had an egress allowlist, but the worker and GitHub sync, which hold the GitHub token, could
reach any host. ADR-014 and ADR-016 close known ways into a worker. This limits what a way we haven't
found could do.

## Decision
- **An internal `workers` network,** with no route out. Workers, GitHub sync, the launcher, the model
  proxy and the API are on it. Workers can't reach the database, which sits on `backend` with the API
  and sync.
- **A second allowlist proxy,** `worker-egress-proxy`, is the only way out: `github.com` and
  `api.github.com`. It's separate from the sandbox's proxy, whose list must never include GitHub.
- **`make check-boundaries`** tests this from inside real worker and sync containers.

## Consequences
- Stolen credentials can't be sent to an arbitrary host from a worker.
- **GitHub itself remains a way out.** The proxy filters by host, so it can't tell the project's repository
  from another on GitHub. A compromised worker could push data to a repository its token can reach, or use
  another account's token. The fine-grained token's single-repository scope limits the first.
- New hosts a project needs must be added deliberately.
