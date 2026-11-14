"""Deliver notifications to the project's inbox issue (ADR-019).

The writer can do two things: open the inbox issue once, and comment on it. It has no way to read
comments, so nothing anyone writes on the issue can reach Axiom. Comment text comes only from
`notifications.comment_text`: fixed sentences, a checked key, a time.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from axiom.orchestrator import notifications
from axiom.state.db import Conn

log = logging.getLogger("axiom.sync.inbox")

INBOX_TITLE = "Axiom inbox"
INBOX_BODY = (
    "Axiom posts here when something needs you: a plan to approve, a question, an escalation, "
    "a pull request ready to merge, or a reminder. Each comment says only what happened and to "
    "which task; the details are on your Axiom dashboard.\n\n"
    "**Axiom never reads replies here.** Answer, approve and merge as usual: through the Axiom "
    "API, and with GitHub's merge button.\n\n"
    "To be emailed about these comments, keep watching this repository, and if the token Axiom "
    "uses is your own, turn on *Settings → Notifications → Email notification preferences → "
    "Include your own updates*."
)


class InboxWriter(Protocol):
    def open_issue(self, owner: str, repo: str, title: str, body: str) -> int: ...
    def comment(self, owner: str, repo: str, number: int, body: str) -> None: ...


class GitHubWriteError(Exception):
    pass


@dataclass
class GitHubInboxWriter:
    token: str
    api: str = "https://api.github.com"
    timeout: int = 30

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            self.api + path,
            data=json.dumps(data).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "axiom-sync",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raise GitHubWriteError(f"GitHub POST {path} -> {e.code}") from None
        except OSError as e:
            raise GitHubWriteError(f"GitHub POST {path}: {e}") from None

    def open_issue(self, owner: str, repo: str, title: str, body: str) -> int:
        return int(
            self._post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})["number"]
        )

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._post(f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})


def _owner_repo(repo_url: str) -> tuple[str, str]:
    path = repo_url.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    owner, _, repo = path.partition("/")
    if not owner or not repo or "/" in repo:
        raise GitHubWriteError(f"not a GitHub repository: {repo_url!r}")
    return owner, repo


def deliver(conn: Conn, writer: InboxWriter, limit: int = 20) -> dict[int, str]:
    """Post what's waiting, a credential exposure first. A failure is recorded and retried next
    pass; it never blocks the others."""
    results: dict[int, str] = {}
    inbox: dict[int, int] = {}
    for n in notifications.to_deliver(conn, limit):
        try:
            owner, repo = _owner_repo(n["repo_url"])
            number = inbox.get(n["project_id"]) or n["inbox_issue"]
            if number is None:
                number = writer.open_issue(owner, repo, INBOX_TITLE, INBOX_BODY)
                notifications.set_inbox_issue(conn, n["project_id"], number)
            inbox[n["project_id"]] = number
            writer.comment(owner, repo, number, notifications.comment_text(n))
            notifications.mark_delivered(conn, n["id"])
            results[n["id"]] = "delivered"
        except (GitHubWriteError, ValueError) as e:
            notifications.mark_failed(conn, n["id"], str(e))
            results[n["id"]] = f"error: {e}"
            log.warning("notification %s not delivered: %s", n["id"], e)
    return results
