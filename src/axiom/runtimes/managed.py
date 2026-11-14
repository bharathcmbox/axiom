"""A second runtime: reviews on Claude Managed Agents (Phase 5 T6, a spike; ADR-020).

The same `ReviewRuntime` interface as the sandbox runtime, so nothing in orchestration changes.
What it keeps from ADR-008/ADR-015 and how:

- **No credentials go in.** The repository at the reviewed commit is uploaded as read-only files
  (`/mnt/session/uploads/repo/...`), so no GitHub token is sent anywhere. The environment is
  `limited` with no hosts, no package managers and no MCP servers: the sandbox has no way out.
- **Read-only.** The agent toolset has only `read`, `glob` and `grep` enabled; uploads are mounted
  read-only anyway.
- **A strict contract.** The review comes back as the input of a custom tool, `submit_review`,
  whose schema is the role's review schema; it's validated with `parse_review` like any other.
- **A hard spend cap and a metered cost.** Each session has a platform-enforced `budget` (the run
  budget), and the cost recorded is the session's `usage.list_cost`.

What it can't keep, written down in ADR-020: the process that drives sessions holds the Anthropic
key (here, the admin's drill process), and the code and diff are sent to Anthropic's servers.
"""

import json
import logging
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.contracts import REVIEW_CHECKLISTS, InvalidResult, parse_review, review_json_schema
from axiom.runtimes.base import EventCallback, Outcome
from axiom.runtimes.review import ReviewOutcome, ReviewRequest
from axiom.sandbox.review_prompt import build_review_prompt

log = logging.getLogger("axiom.runtimes.managed")

REPO_MOUNT = "/repo"  # under /mnt/session/uploads
REPO_PATH = "/mnt/session/uploads" + REPO_MOUNT
SUBMIT_TOOL = "submit_review"
MAX_SUBMISSIONS = 3  # a custom tool's schema isn't enforced: an invalid review is sent back
READ_TOOLS = ("read", "glob", "grep")
ALL_TOOLS = ("bash", "read", "write", "edit", "glob", "grep", "web_fetch", "web_search")
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 900  # the platform allows 999 file resources per session
SYSTEM = (
    "You are an independent code reviewer working for Axiom. You can read and search files "
    f"(read, glob, grep); you cannot change anything or run commands. The repository, at the "
    f"commit under review, is mounted read-only at {REPO_PATH}. When you have finished, call "
    f"the `{SUBMIT_TOOL}` tool exactly once with your structured review, then stop."
)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """The review schema with every `$ref` to `$defs` replaced by its definition, for a custom
    tool's `input_schema` (no `$defs`)."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def agent_config(role: str, model: str) -> dict[str, Any]:
    """The Managed Agents agent for one reviewer role and model: read-only tools and the
    submit tool, nothing else."""
    return {
        "name": f"axiom-{role.replace('_', '-')}-{model}",
        "model": model,
        "system": SYSTEM,
        "tools": [
            {
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": [
                    {"name": t, "enabled": t in READ_TOOLS,
                     "permission_policy": {"type": "always_allow"}} if t in READ_TOOLS
                    else {"name": t, "enabled": False}
                    for t in ALL_TOOLS
                ],
            },
            {
                "type": "custom",
                "name": SUBMIT_TOOL,
                "description": "Submit your review: verdict, summary, checklist and findings.",
                "input_schema": inline_refs(review_json_schema(role)),
            },
        ],
        "metadata": {"axiom": "phase-5-t6", "role": role},
    }  # fmt: skip


ENVIRONMENT_CONFIG: dict[str, Any] = {
    "type": "cloud",
    "networking": {"type": "limited", "allow_package_managers": False,
                   "allow_mcp_servers": False, "allowed_hosts": []},
}  # fmt: skip


def repo_files(workspace: Path) -> list[Path]:
    """Tracked files worth reading, at most MAX_FILES, each at most MAX_FILE_BYTES."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=workspace, capture_output=True,
                         check=True).stdout.decode()  # fmt: skip
    files = [p for p in out.split("\0") if p and not p.startswith(".git/")]
    kept = [Path(p) for p in files if (workspace / p).is_file()
            and (workspace / p).stat().st_size <= MAX_FILE_BYTES]  # fmt: skip
    return kept[:MAX_FILES]


def prompt_for(request: ReviewRequest, guidance: str | None) -> str:
    return (
        build_review_prompt(request, guidance)
        + f"\n\nThe repository is at {REPO_PATH}. Finish by calling `{SUBMIT_TOOL}` once."
    )


def cents_to_usd(amount: Any) -> Decimal | None:
    try:
        return (Decimal(str(amount)) / 100).quantize(Decimal("0.0001"))
    except ArithmeticError, ValueError, TypeError:
        return None


@dataclass
class SessionResult:
    contract_input: Any = None
    rejected: list[str] = field(default_factory=list)  # why earlier submissions were sent back
    error: str | None = None
    budget_reached: bool = False
    timed_out: bool = False
    tool_calls: int = 0


def drive(
    events: Iterable[Any],
    send: Callable[[list[dict[str, Any]]], None],
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    validate: Callable[[Any], str | None] = lambda _: None,
) -> SessionResult:
    """Follow one session's events until it's done. Takes the first valid `submit_review`; an
    invalid one goes back with its problems, up to MAX_SUBMISSIONS in all (the platform doesn't
    enforce a custom tool's schema). Denies any call that waits for approval (nobody is
    watching), and stops at the deadline."""
    result = SessionResult()
    for event in events:
        kind = getattr(event, "type", None)
        if clock() > deadline:
            result.timed_out = True
            send([{"type": "user.interrupt"}])
            break
        if kind == "agent.custom_tool_use":
            result.tool_calls += 1
            submitted = getattr(event, "input", None)
            if getattr(event, "name", None) != SUBMIT_TOOL or result.contract_input is not None:
                reply = f"Only {SUBMIT_TOOL} is available, and only once."
            elif (problem := validate(submitted)) is None:
                result.contract_input = submitted
                reply = "Review recorded. Stop now."
            elif len(result.rejected) + 1 >= MAX_SUBMISSIONS:
                result.contract_input = submitted  # the last try: recorded, and fails validation
                reply = "Review recorded. Stop now."
            else:
                result.rejected.append(problem)
                reply = (
                    f"That review is invalid: {problem}. Call {SUBMIT_TOOL} again with "
                    "verdict, summary, checklist (an object keyed by checklist item) and "
                    "findings (a list), exactly as the tool's schema says."
                )
            send([{"type": "user.custom_tool_result", "custom_tool_use_id": event.id,
                   "content": [{"type": "text", "text": reply}]}])  # fmt: skip
        elif kind in ("agent.tool_use", "agent.mcp_tool_use"):
            result.tool_calls += 1
            if getattr(event, "evaluated_permission", None) == "ask":
                send([{"type": "user.tool_confirmation", "tool_use_id": event.id,
                       "result": "deny", "deny_message": "No approvals in a review."}])  # fmt: skip
        elif kind == "session.status_idle":
            reason = getattr(getattr(event, "stop_reason", None), "type", None)
            if reason == "budget_reached":
                result.budget_reached = True
                break
            if reason != "requires_action":
                break
        elif kind == "session.status_terminated":
            break
        elif kind == "session.error":
            result.error = str(getattr(event, "error", "session error"))[:500]
    return result


def outcome_of(result: SessionResult, role: str, cost: Decimal | None, tokens: tuple[
        int | None, int | None], transcript: Path | None) -> ReviewOutcome:  # fmt: skip
    common: dict[str, Any] = {"cost_usd": cost, "input_tokens": tokens[0],
                              "output_tokens": tokens[1], "turns": result.tool_calls,
                              "transcript_path": transcript}  # fmt: skip
    if result.contract_input is not None:
        try:
            contract = parse_review(result.contract_input, role)
        except InvalidResult as e:
            return ReviewOutcome(Outcome.FAILED, error=f"invalid review contract: {e}", **common)
        return ReviewOutcome(Outcome.COMPLETED, contract=contract, **common)
    if result.budget_reached:
        return ReviewOutcome(Outcome.BUDGET_EXCEEDED, error="session budget reached", **common)
    if result.timed_out:
        return ReviewOutcome(Outcome.FAILED, error="review timed out", **common)
    return ReviewOutcome(Outcome.FAILED, error=result.error or "no review submitted", **common)


def _problem(contract: Any, role: str) -> str | None:
    try:
        parse_review(contract, role)
    except InvalidResult as e:
        return str(e)[:1000]
    return None


@dataclass
class ManagedReviewRuntime:
    """Reviews on Managed Agents. `client` is an `anthropic.Anthropic`; `ids` caches the
    environment and one agent per role and model (created once, then reused)."""

    client: Any
    ids_file: Path
    timeout_s: float = 900
    name: str = "managed-agents"
    _ids: dict[str, str] = field(default_factory=dict)

    def cancel(self) -> None:  # sessions are interrupted at their deadline
        pass

    def _load(self) -> None:
        if not self._ids and self.ids_file.exists():
            self._ids = json.loads(self.ids_file.read_text())

    def _save(self) -> None:
        self.ids_file.parent.mkdir(parents=True, exist_ok=True)
        self.ids_file.write_text(json.dumps(self._ids, indent=2))

    def environment_id(self) -> str:
        self._load()
        if "environment" not in self._ids:
            env = self.client.beta.environments.create(name="axiom-review-readonly",
                                                       config=ENVIRONMENT_CONFIG)  # fmt: skip
            self._ids["environment"] = env.id
            self._save()
        return self._ids["environment"]

    def agent_id(self, role: str, model: str) -> str:
        self._load()
        key = f"agent:{role}:{model}"
        if key not in self._ids:
            agent = self.client.beta.agents.create(**agent_config(role, model))
            self._ids[key] = agent.id
            self._save()
        return self._ids[key]

    def _upload(self, workspace: Path) -> list[dict[str, Any]]:
        resources = []
        for rel in repo_files(workspace):
            with (workspace / rel).open("rb") as f:
                uploaded = self.client.files.upload(file=(rel.name, f))
            resources.append({"type": "file", "file_id": uploaded.id,
                              "mount_path": f"{REPO_MOUNT}/{rel.as_posix()}"})  # fmt: skip
        return resources

    def run_review(
        self, request: ReviewRequest, on_event: EventCallback | None = None
    ) -> ReviewOutcome:
        if request.role not in REVIEW_CHECKLISTS:
            return ReviewOutcome(Outcome.FAILED, error=f"unknown role {request.role}")
        model = request.model or "claude-opus-5-5"
        guidance_path = request.workspace / "CLAUDE.md"
        guidance = guidance_path.read_text(errors="replace") if guidance_path.is_file() else None
        cents = str(max(int(request.budget_usd * 100), 1))
        session = self.client.beta.sessions.create(
            agent=self.agent_id(request.role, model),
            environment_id=self.environment_id(),
            title=f"Axiom review {request.task_key} {request.role}",
            resources=self._upload(request.workspace),
            budget={"type": "limit", "max_list_cost": {"amount": cents, "currency": "USD"}},
            metadata={"task": request.task_key, "role": request.role},
        )
        transcript = request.output_dir / "managed_session.json"
        request.output_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s

        def send(events: list[dict[str, Any]]) -> None:
            self.client.beta.sessions.events.send(session_id=session.id, events=events)

        with self.client.beta.sessions.events.stream(session_id=session.id) as stream:
            text = prompt_for(request, guidance)
            send([{"type": "user.message", "content": [{"type": "text", "text": text}]}])
            result = drive(
                stream, send, deadline=deadline, validate=lambda c: _problem(c, request.role)
            )
        final = self.client.beta.sessions.retrieve(session_id=session.id)
        usage = getattr(final, "usage", None)
        list_cost = getattr(usage, "list_cost", None)
        cost = cents_to_usd(getattr(list_cost, "amount", None))
        tokens = (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
        transcript.write_text(json.dumps({"session_id": session.id, "status": final.status,
                                          "cost_usd": str(cost), "tool_calls": result.tool_calls,
                                          "rejected": result.rejected}, indent=2))  # fmt: skip
        return outcome_of(result, request.role, cost, tokens, transcript)
