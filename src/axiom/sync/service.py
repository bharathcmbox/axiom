"""The GitHub sync: map what happens on GitHub to task commands (ADR-010). Deterministic, read-only
towards GitHub, and the only bridge from GitHub back into Axiom.

For every task in IN_REVIEW or AWAITING_MERGE with a pull request:

- PR merged by a human           -> merge (DONE), actor human:github-<login>
    ...while still IN_REVIEW     -> approve (human override) + merge, flagged early
- PR closed without merging      -> cancel
- "Request changes" review on the submitted commit
                                 -> reject_merge / request_changes (human)
- `/axiom changes <text>` comment by owner/collaborator
                                 -> the same, with <text> as feedback
- CI runs for the submitted commit -> record_ci, then the aggregator decides

Why the comment command: PRs are opened with the owner's token, and GitHub does not let a PR's
author "request changes" on it. A comment works for everyone.
"""

import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import IllegalTransition, TaskCommand, TaskState
from axiom.errors import AxiomError
from axiom.orchestrator.aggregator import record_ci
from axiom.orchestrator.commands import apply_locked, lock_task
from axiom.state.db import Conn, connect
from axiom.sync.github import GitHubReader, GitHubReadError, parse_pr_url

log = logging.getLogger("axiom.sync")
SYNC = Actor(ActorKind.SYSTEM, "github-sync")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CHANGES_COMMAND = re.compile(r"^\s*/axiom\s+changes\b[:\s]*(?P<text>.*)", re.IGNORECASE | re.DOTALL)
CI_OK = frozenset({"success", "skipped", "neutral"})


def github_actor(login: str | None) -> Actor:
    safe = re.sub(r"[^A-Za-z0-9-]", "", login or "") or "unknown"
    return Actor(ActorKind.HUMAN, f"github-{safe}")


def ci_status(runs: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """Fold the workflow runs for one commit into pending / success / failure (None: no runs)."""
    if not runs:
        return None, {}
    summary = "; ".join(
        f"{r.get('name', 'ci')}: {r.get('conclusion') or r.get('status')}" for r in runs
    )
    details = {"summary": summary, "url": runs[0].get("html_url")}
    if any(r.get("status") != "completed" for r in runs):
        return "pending", details
    failed = [r for r in runs if r.get("conclusion") not in CI_OK]
    return ("failure" if failed else "success"), details


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _last_submit(conn: Conn, task_id: int) -> datetime | None:
    row = conn.execute(
        """SELECT max(occurred_at) AS t FROM events
           WHERE task_id = %s AND type = 'TASK_SUBMITTED'""",
        (task_id,),
    ).fetchone()
    return row["t"] if row else None


def _human_change_request(
    conn: Conn, task: dict[str, Any], reviews: list[dict[str, Any]], comments: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """(login, text) of the latest human request for changes on the submitted work, if any."""
    submitted = _last_submit(conn, task["id"])
    found: tuple[datetime, str, str] | None = None
    for r in reviews:
        if r.get("state") == "CHANGES_REQUESTED" and r.get("commit_id") == task["head_sha"]:
            when = _ts(r["submitted_at"])
            if found is None or when > found[0]:
                found = (when, r["user"]["login"], r.get("body") or "changes requested")
    for c in comments:
        m = CHANGES_COMMAND.match(c.get("body") or "")
        if not m or c.get("author_association") not in TRUSTED_ASSOCIATIONS:
            continue
        when = _ts(c["created_at"])
        if submitted is not None and when <= submitted:
            continue  # about an earlier round
        if found is None or when > found[0]:
            found = (when, c["user"]["login"], m.group("text").strip() or "changes requested")
    return (found[1], found[2]) if found else None


def sync_task(conn: Conn, reader: GitHubReader, key: str) -> str:
    """Bring one task in step with its PR. Returns what was done (for logs and tests)."""
    task = conn.execute("SELECT * FROM tasks WHERE key = %s", (key,)).fetchone()
    assert task is not None
    owner, repo, number = parse_pr_url(task["pull_request_url"])
    pr = reader.pull_request(owner, repo, number)

    if pr.get("merged"):
        actor = github_actor((pr.get("merged_by") or {}).get("login"))
        with conn.transaction():
            row = lock_task(conn, key)
            early = row["state"] == TaskState.IN_REVIEW
            if early:  # merged on GitHub before Axiom's gate finished: a human decision
                apply_locked(
                    conn,
                    row,
                    TaskCommand.APPROVE,
                    actor,
                    payload={"source": "github", "merged_before_gate": True},
                )
                row = lock_task(conn, key)
            apply_locked(
                conn,
                row,
                TaskCommand.MERGE,
                actor,
                payload={
                    "source": "github",
                    "pr": number,
                    "merge_commit": pr.get("merge_commit_sha"),
                    "merged_before_gate": early,
                },
            )
        return "merged (early)" if early else "merged"

    if pr.get("state") == "closed":
        with conn.transaction():
            row = lock_task(conn, key)
            payload = {"source": "github", "pr": number, "reason": "PR closed"}
            apply_locked(conn, row, TaskCommand.CANCEL, github_actor(None), payload=payload)
        return "cancelled"

    request = _human_change_request(
        conn, task, reader.reviews(owner, repo, number), reader.comments(owner, repo, number)
    )
    if request:
        login, text = request
        with conn.transaction():
            row = lock_task(conn, key)
            if row["state"] == TaskState.AWAITING_MERGE:
                command = TaskCommand.REJECT_MERGE
            else:
                command = TaskCommand.REQUEST_CHANGES
            payload = {"source": "github", "pr": number, "feedback": [f"{login} on GitHub: {text}"]}
            apply_locked(conn, row, command, github_actor(login), payload=payload)
        return f"changes requested by {login}"

    if task["head_sha"]:
        status, details = ci_status(reader.workflow_runs(owner, repo, task["head_sha"]))
        if status:
            decision = record_ci(
                conn, SYNC, key, sha=task["head_sha"], status=status, details=details
            )
            return f"ci {status}: {decision}"
    return "nothing to do"


def sync_once(conn: Conn, reader: GitHubReader) -> dict[str, str]:
    """One pass over every task waiting on GitHub. One task's failure never blocks the others."""
    keys = [r["key"] for r in conn.execute(
        """SELECT key FROM tasks WHERE state IN ('IN_REVIEW', 'AWAITING_MERGE')
             AND pull_request_url IS NOT NULL ORDER BY id"""
    ).fetchall()]  # fmt: skip
    results = {}
    for key in keys:
        try:
            results[key] = sync_task(conn, reader, key)
        except (GitHubReadError, AxiomError, IllegalTransition) as e:
            results[key] = f"error: {e}"
            log.warning("%s: sync failed: %s", key, e)
    return results


def main() -> int:  # `python -m axiom.sync.service`
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.environ.get("AXIOM_GITHUB_TOKEN")
    if not token:
        raise SystemExit("AXIOM_GITHUB_TOKEN is required")
    interval = float(os.environ.get("AXIOM_SYNC_INTERVAL_SECONDS", "30"))
    reader = GitHubReader(token=token)
    log.info("github sync started (every %.0fs)", interval)
    while True:
        try:
            with connect(get_settings().database_url) as conn:
                for key, result in sync_once(conn, reader).items():
                    if result != "nothing to do":
                        log.info("%s: %s", key, result)
        except Exception:
            log.exception("sync pass failed")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
