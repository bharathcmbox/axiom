"""Read-only GitHub access for the sync service. It can look; it cannot change anything.

The sync's one write, comments on the inbox issue, is `sync/inbox.py` (ADR-019)."""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?$")


class GitHubReadError(Exception):
    pass


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = _PR_URL.match(url)
    if not m:
        raise GitHubReadError(f"not a GitHub pull request URL: {url!r}")
    return m.group(1), m.group(2), int(m.group(3))


@dataclass
class GitHubReader:
    token: str
    api: str = "https://api.github.com"
    timeout: int = 30

    def _get(self, path: str) -> Any:
        req = urllib.request.Request(
            self.api + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "axiom-sync",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raise GitHubReadError(f"GitHub GET {path} -> {e.code}") from None
        except OSError as e:
            raise GitHubReadError(f"GitHub GET {path}: {e}") from None

    def pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}")  # type: ignore[no-any-return]

    def reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100")  # type: ignore[no-any-return]

    def comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._get(f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100")  # type: ignore[no-any-return]

    def closed_by(self, owner: str, repo: str, number: int) -> str | None:
        """Who closed a PR: a PR's own data doesn't say, but its events do."""
        events = self._get(f"/repos/{owner}/{repo}/issues/{number}/events?per_page=100")
        closes = [e for e in events if e.get("event") == "closed"]
        return (closes[-1].get("actor") or {}).get("login") if closes else None

    def workflow_runs(self, owner: str, repo: str, head_sha: str) -> list[dict[str, Any]]:
        data = self._get(f"/repos/{owner}/{repo}/actions/runs?head_sha={head_sha}&per_page=50")
        return list(data.get("workflow_runs", []))
