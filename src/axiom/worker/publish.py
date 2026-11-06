"""Publish verified work: push an `axiom/*` branch and open (or reuse) a pull request.

ADR-007: on a free private repository GitHub cannot stop a token from pushing to `main` or
merging, so this module is the guarantee:
- `push_branch` only ever pushes HEAD to `refs/heads/axiom/...`, never with --force.
- The GitHub client can list and create pull requests. It has no merge, no ref update, no
  settings call: those code paths do not exist.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from axiom.workspaces.git import git
from axiom.workspaces.manager import BRANCH_PREFIX

_SAFE_BRANCH = re.compile(r"^axiom/[A-Z][A-Z0-9]*-[0-9]+-e[0-9]+$")
_GITHUB_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class PublishError(Exception):
    pass


def assert_safe_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX) or not _SAFE_BRANCH.match(branch):
        raise PublishError(f"refusing to push to {branch!r}: only axiom/<TASK>-e<N> branches")


def push_branch(repo_dir: Any, branch: str, token: str | None, url: str) -> None:
    assert_safe_branch(branch)
    # Explicit URL and refspec: the local HEAD goes to exactly this branch of the project's own
    # repository, wherever a remote may have been pointed. No --force.
    git(["push", "--quiet", url, f"HEAD:refs/heads/{branch}"], cwd=repo_dir, token=token)


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    m = _GITHUB_URL.match(repo_url)
    if not m:
        raise PublishError(f"not a GitHub HTTPS repository URL: {repo_url!r}")
    return m.group(1), m.group(2)


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    created: bool


@dataclass
class GitHubClient:
    """The three GitHub calls Axiom's worker makes: find PR, create PR, comment. Nothing that
    could merge, push to a protected ref or change settings."""

    token: str
    api: str = "https://api.github.com"
    timeout: int = 30

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.api + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "axiom-worker",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise PublishError(f"GitHub {method} {path} -> {e.code}: {detail}") from None

    def find_open_pr(self, owner: str, repo: str, branch: str) -> PullRequest | None:
        pulls = self._call("GET", f"/repos/{owner}/{repo}/pulls?state=open&head={owner}:{branch}")
        if pulls:
            return PullRequest(pulls[0]["number"], pulls[0]["html_url"], created=False)
        return None

    def create_pr(
        self, owner: str, repo: str, *, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        pr = self._call(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body, "draft": False},
        )
        return PullRequest(pr["number"], pr["html_url"], created=True)

    def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        """A conversation comment on the PR (PR comments use the issues endpoint)."""
        self._call("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})


def comment_on_pull_request(client: GitHubClient, repo_url: str, number: int, body: str) -> None:
    owner, repo = parse_github_repo(repo_url)
    client.comment(owner, repo, number, body)


def open_pull_request(
    client: GitHubClient, repo_url: str, *, branch: str, base: str, title: str, body: str
) -> PullRequest:
    """Idempotent: a retried execution reuses its open PR instead of opening a second one."""
    assert_safe_branch(branch)
    owner, repo = parse_github_repo(repo_url)
    existing = client.find_open_pr(owner, repo, branch)
    if existing:
        return existing
    return client.create_pr(owner, repo, branch=branch, base=base, title=title, body=body)


def pr_body(
    *,
    task_key: str,
    title: str,
    agent: str,
    execution_id: int,
    result: dict[str, Any],
    verified_tests: dict[str, Any] | None,
    cost_usd: str | None,
) -> str:
    lines = [
        f"**{task_key}: {title}**",
        "",
        result.get("summary", ""),
        "",
        "### Acceptance criteria (agent's claims)",
        "| Criterion | Met | Evidence |",
        "|---|---|---|",
    ]
    for c in result.get("criteria", []):
        lines.append(f"| {c['criterion']} | {'✅' if c['met'] else '❌'} | {c['evidence']} |")
    lines += ["", "### Tests re-run by Axiom (verified)"]
    if verified_tests:
        status = "✅ passed" if verified_tests["passed"] else "❌ failed"
        lines.append(
            f"`{verified_tests['command']}`: {status} (exit {verified_tests['exit_code']})"
        )
    else:
        lines.append("No test command configured for this project.")
    if result.get("known_limitations"):
        lines += ["", "### Known limitations"] + [f"- {x}" for x in result["known_limitations"]]
    lines += [
        "",
        "---",
        f"Opened by Axiom · agent `{agent}` · execution e{execution_id} · "
        f"cost ${cost_usd or '?'} · review required, Axiom never merges.",
    ]
    return "\n".join(lines)
