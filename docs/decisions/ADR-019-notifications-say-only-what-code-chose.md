# ADR-019: Notifications: an outbox, fixed text, and replies are never commands

Status: Accepted (2026-11-13). Revises ADR-010: the sync may write comments on one issue.

## Context
Unattended operation only works if Axiom tells the human when it needs them: a plan to approve, a question, an
escalation, a PR ready to merge, a refused budget, a potential credential exposure. Today the human finds out by
opening the dashboard. Email or Slack would need a new secret and new egress. GitHub already emails people about
comments, and the sync already reaches GitHub. But the project repositories are public, so anything posted
there can be read by anyone, and anyone can reply.

## Decision
- **An outbox.** A `notifications` table: subject, reason, dedup key, created, delivered. A row is written in
  the same transaction as the event that causes it, so a notification can't be lost, and none exists without
  its event. One row per reason per subject, so reminders don't repeat every tick.
- **Delivery.** The dashboard shows every notification. The sync service posts "needs you" reasons as comments
  on one pinned `Axiom inbox` issue in the project repository, and marks them delivered. Potential credential
  exposure is always pushed, and first. Everything else is dashboard-only to start: quiet by default.
- **Fixed text only.** A comment holds text chosen by code: the reason, the task or plan key, the time. Never the
  agent's question, a plan, an error or anything else a model wrote. That text could carry injected
  instructions or a leaked value, and the repository is public.
- **Replies are never commands.** Axiom never reads comments on the inbox issue. Humans answer, approve, retry
  and cancel through the API with their own token, and merge on GitHub, as before.
- **Token.** The sync token gets Issues: write, on the project repository only. The worker token doesn't change.

## Consequences
- The human hears about everything that needs them, without a new secret or a new host.
- ADR-010's "the sync only reads GitHub" becomes "the sync reads GitHub and writes comments on the inbox issue".
  It still has no merge code path, and nothing it writes can change Axiom's state.
- A stolen sync token could now also write issue comments in that repository: spam, not a way to change state.
- If a team joins or a repository is private, email or Slack can be added as another delivery, with its own
  ADR for the new egress.
