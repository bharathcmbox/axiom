"""Phase 4 T9: developers in parallel, conflicts resolved by rework, and QA after the work. The whole
path from a plan to merged tasks: real API, git and sync. Fake: the models and GitHub's API."""

import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.contracts import parse_plan, parse_result
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.orchestrator import plans
from axiom.orchestrator.tasks import get_task
from axiom.registry.projects import set_test_paths
from axiom.runtimes.base import Outcome, RunOutcome, RunRequest
from axiom.runtimes.fake import FakeRuntime
from axiom.runtimes.fake_plan import FakePlanner
from axiom.runtimes.fake_review import FakeReviewer
from axiom.sandbox.prompt import QA_ROLE, build_prompt
from axiom.state.db import Conn
from axiom.state.invariants import check_invariants
from axiom.sync.service import sync_once
from axiom.worker.verify import unresolved_conflicts
from axiom.workspaces.manager import prepare_workspace
from tests.integration.helpers import HUMAN, do, events_for
from tests.integration.test_plan_worker import _worker as plan_worker
from tests.integration.test_review_worker import reviewer
from tests.integration.workers_shared import (
    FakePrOpener,
    make_project,
    make_task,
    make_worker,
    run_git,
)

BASE = "# guide\n"
OURS = "# guide, from the branch\n"
THEIRS = "# guide, from main\n"
RESOLVED = "# guide, with both\n"


def _commit_to_main(remote: Path, tmp_path: Path, path: str, text: str, name: str = "main") -> None:
    work = tmp_path / f"push-{name}"
    run_git("git", "clone", "-q", str(remote), str(work), cwd=tmp_path)
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    run_git("git", "add", "-A", cwd=work)
    run_git("git", "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "human", cwd=work)
    run_git("git", "push", "-q", "origin", "HEAD:main", cwd=work)


def _merge_on_remote(remote: Path, tmp_path: Path, branch: str) -> None:
    work = tmp_path / f"merge-{branch.replace('/', '-')}"
    run_git("git", "clone", "-q", str(remote), str(work), cwd=tmp_path)
    run_git("git", "-c", "user.name=h", "-c", "user.email=h@h", "merge", "-q", "--no-edit",
            f"origin/{branch}", cwd=work)  # fmt: skip
    run_git("git", "push", "-q", "origin", "HEAD:main", cwd=work)


def _on_branch(remote: Path, branch: str, path: str) -> str:
    return run_git("git", "show", f"{branch}:{path}", cwd=remote)


class FakeGitHubState:
    """GitHub as the sync sees it, per pull request."""

    def __init__(self) -> None:
        self.merged: set[int] = set()
        self.conflicted: set[int] = set()

    def pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        if number in self.merged:
            return {"state": "closed", "merged": True, "merged_by": {"login": "bharathcmbox"}}
        return {"state": "open", "merged": False, "mergeable": number not in self.conflicted}

    def reviews(self, *a: Any) -> list[dict[str, Any]]:
        return []

    def comments(self, *a: Any) -> list[dict[str, Any]]:
        return []

    def workflow_runs(self, *a: Any) -> list[dict[str, Any]]:
        return []

    def closed_by(self, *a: Any) -> str:
        return "bharathcmbox"


def _number(db: Conn, key: str) -> int:
    url = get_task(db, key).pull_request_url
    assert url is not None
    return int(url.rsplit("/", 1)[1])


class EditsItsOwnPath(FakeRuntime):
    """Writes to the first path its task allows, and records what it was asked."""

    def __init__(self, text: str = "edited\n", barrier: threading.Barrier | None = None) -> None:
        super().__init__()
        self.text, self.barrier, self.requests = text, barrier, []

    def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
        self.requests.append(request)
        if self.barrier is not None:
            self.barrier.wait(timeout=20)  # both developers are mid-run, or this breaks
        self.edits = {request.allowed_paths[0]: self.text}
        return super().run(request, on_event)


def _submitted(db: Conn, api_url: str, tmp_path: Path, remote: Path) -> tuple[str, FakePrOpener]:
    make_project(db, remote, test_command=None)
    key = make_task(db)
    opener = FakePrOpener()
    assert make_worker(api_url, tmp_path, FakeRuntime(edits={"CLAUDE.md": OURS}), opener).run_once()
    assert get_task(db, key).state == TaskState.IN_REVIEW
    return key, opener


def _conflict(db: Conn, key: str, remote: Path, tmp_path: Path, text: str = THEIRS) -> None:
    _commit_to_main(remote, tmp_path, "CLAUDE.md", text)
    do(db, key, TaskCommand.REPORT_CONFLICT, payload={"feedback": ["conflict"]})
    assert get_task(db, key).state == TaskState.READY


# ---- merging the default branch on rework ---------------------------------------------------


def test_a_real_conflict_is_handed_to_the_agent_with_its_markers(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    key, opener = _submitted(api_tokens, api_url, tmp_path, remote)
    _conflict(api_tokens, key, remote, tmp_path)
    seen: dict[str, Any] = {}

    class Resolver(FakeRuntime):
        def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
            seen["text"] = (request.workspace / "CLAUDE.md").read_text()
            seen["feedback"] = request.previous_feedback
            return super().run(request, on_event)

    worker = make_worker(
        api_url, tmp_path, Resolver(edits={"CLAUDE.md": RESOLVED}), opener, agent="DEV-02"
    )
    assert worker.run_once()
    assert (
        "<<<<<<<" in seen["text"]
        and THEIRS.strip() in seen["text"]
        and OURS.strip() in seen["text"]
    )
    assert any("conflict markers in: CLAUDE.md" in f for f in seen["feedback"])
    task = get_task(api_tokens, key)
    assert task.state == TaskState.IN_REVIEW and task.review_rounds == 1
    assert task.branch is not None
    assert _on_branch(remote, task.branch, "CLAUDE.md") == RESOLVED.strip()
    # The branch now holds main, so GitHub can merge it. Same branch, same PR.
    assert run_git("git", "merge-base", "--is-ancestor", "main", task.branch, cwd=remote) == ""
    assert len({c[1] for c in opener.calls}) == 1


def test_unresolved_markers_are_never_published(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    key, opener = _submitted(api_tokens, api_url, tmp_path, remote)
    head = get_task(api_tokens, key).head_sha
    _conflict(api_tokens, key, remote, tmp_path)

    class Ignores(FakeRuntime):
        def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
            self.edits = {"other.txt": "something else\n"}
            return super().run(request, on_event)

    worker = make_worker(api_url, tmp_path, Ignores(), opener, agent="DEV-02")
    assert worker.run_once()
    task = get_task(api_tokens, key)
    assert task.state == TaskState.READY
    assert task.branch is not None
    assert run_git("git", "rev-parse", task.branch, cwd=remote) == head  # nothing was pushed
    errors = [e["payload"].get("error", "") for e in events_for(api_tokens, key)]
    assert any("conflict with the default branch not resolved" in str(e) for e in errors)


def test_a_clean_merge_needs_no_further_edit(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """Main moved in another file, so bringing the branch up to date is all there is to do."""
    key, opener = _submitted(api_tokens, api_url, tmp_path, remote)
    _commit_to_main(remote, tmp_path, "unrelated.txt", "from main\n")
    do(api_tokens, key, TaskCommand.REPORT_CONFLICT, payload={"feedback": ["x"]})

    class NoEdits(FakeRuntime):
        def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
            result = parse_result({"status": "submitted", "summary": "Nothing more to change."})
            return RunOutcome(Outcome.COMPLETED, result=result, cost_usd=Decimal("0.10"))

    assert make_worker(api_url, tmp_path, NoEdits(), opener, agent="DEV-02").run_once()
    task = get_task(api_tokens, key)
    assert task.state == TaskState.IN_REVIEW and task.branch is not None
    assert _on_branch(remote, task.branch, "unrelated.txt") == "from main"
    assert _on_branch(remote, task.branch, "CLAUDE.md") == OURS.strip()


def test_main_s_changes_are_not_counted_as_the_agents(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """Main's change is outside the task's allowed paths. It must not fail verification."""
    key, opener = _submitted(api_tokens, api_url, tmp_path, remote)
    _commit_to_main(remote, tmp_path, "src/far/away.py", "x = 1\n")
    do(api_tokens, key, TaskCommand.REPORT_CONFLICT, payload={"feedback": ["x"]})
    worker = make_worker(
        api_url, tmp_path, FakeRuntime(edits={"CLAUDE.md": RESOLVED}), opener, agent="DEV-02"
    )
    assert worker.run_once()
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW


def test_a_first_run_isnt_merged(remote: Path, tmp_path: Path) -> None:
    ws = prepare_workspace(
        tmp_path / "w", task_key="AX-1", execution_id=1, repo_url=str(remote),
        default_branch="main", token=None,
    )  # fmt: skip
    assert ws.merged_default is False and ws.conflicted == ()


def test_an_up_to_date_branch_has_nothing_to_merge(remote: Path, tmp_path: Path) -> None:
    run_git("git", "branch", "axiom/AX-1-e1", "main", cwd=remote)
    ws = prepare_workspace(
        tmp_path / "w", task_key="AX-1", execution_id=2, repo_url=str(remote),
        default_branch="main", token=None, existing_branch="axiom/AX-1-e1",
    )  # fmt: skip
    assert ws.continued and ws.merged_default is False


# ---- what counts as resolved ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n", "ok\n>>>>>>> FETCH_HEAD\n", "<<<<<<<\n"]
)
def test_markers_are_noticed(tmp_path: Path, text: str) -> None:
    (tmp_path / "f").write_text(text)
    assert unresolved_conflicts(tmp_path, ("f",)) == ["f"]


@pytest.mark.parametrize("text", ["clean\n", "a <<<<<<< b\n", "========\n", "x = 1 <<<<<<< 2\n"])
def test_clean_files_pass(tmp_path: Path, text: str) -> None:
    (tmp_path / "f").write_text(text)
    assert unresolved_conflicts(tmp_path, ("f",)) == []


def test_deleting_a_file_resolves_it(tmp_path: Path) -> None:
    assert unresolved_conflicts(tmp_path, ("gone",)) == []


def test_a_symlink_isnt_followed_or_accepted(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("clean\n")
    (tmp_path / "f").symlink_to(tmp_path / "secret")
    assert unresolved_conflicts(tmp_path, ("f",)) == ["f"]


# ---- report_conflict ------------------------------------------------------------------------


def test_only_the_system_reports_a_conflict(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    from axiom.domain.task_machine import ActorNotPermitted

    key, _ = _submitted(api_tokens, api_url, tmp_path, remote)
    for actor in (HUMAN, Actor(ActorKind.AGENT, "DEV-01")):
        with pytest.raises(ActorNotPermitted):
            do(api_tokens, key, TaskCommand.REPORT_CONFLICT, actor)
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW


# ---- QA tasks -------------------------------------------------------------------------------


def _qa_task(db: Conn, **kwargs: Any) -> str:
    return make_task(db, assigned_role="qa", allowed_paths=["tests/**"], **kwargs)


def test_a_qa_agent_takes_a_qa_task_and_gets_its_role_in_the_prompt(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    make_project(api_tokens, remote, test_command=None)
    key = _qa_task(api_tokens)

    class Allowed(EditsItsOwnPath):
        def run(self, request: RunRequest, on_event: Any = None) -> RunOutcome:
            self.requests.append(request)
            self.edits = {"tests/test_x.py": self.text}
            return FakeRuntime.run(self, request, on_event)

    allowed = Allowed("def test_it(): pass\n")
    assert make_worker(api_url, tmp_path, allowed, FakePrOpener(), agent="QA-01").run_once()
    assert get_task(api_tokens, key).state == TaskState.IN_REVIEW
    request = allowed.requests[0]
    assert request.role == "qa" and QA_ROLE in build_prompt(request, None)


def test_a_developers_prompt_has_no_qa_role(tmp_path: Path) -> None:
    request = RunRequest(
        task_key="AX-1", title="t", specification="s", acceptance_criteria=[], allowed_paths=[],
        workspace=tmp_path, output_dir=tmp_path, budget_usd=Decimal(1),
    )  # fmt: skip
    assert QA_ROLE not in build_prompt(request, None)


def test_developers_dont_take_qa_tasks_and_qa_doesnt_take_developer_tasks(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    make_project(api_tokens, remote, test_command=None)
    qa = _qa_task(api_tokens)
    assert make_worker(api_url, tmp_path, FakeRuntime(), FakePrOpener()).run_once() is False
    assert get_task(api_tokens, qa).state == TaskState.READY
    dev = make_task(api_tokens)
    runtime = FakeRuntime(edits={"tests/test_x.py": "x\n"})
    assert make_worker(api_url, tmp_path, runtime, FakePrOpener(), agent="QA-01").run_once()
    assert get_task(api_tokens, dev).state == TaskState.READY  # QA took its own


def test_a_qa_agent_cannot_change_the_code_it_tests(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    make_project(api_tokens, remote, test_command=None)
    key = _qa_task(api_tokens)
    opener = FakePrOpener()
    runtime = FakeRuntime(edits={"app.py": "weakened\n"})
    assert make_worker(api_url, tmp_path, runtime, opener, agent="QA-01").run_once()
    assert get_task(api_tokens, key).state == TaskState.READY and opener.calls == []
    assert any(
        "outside allowed_paths" in str(e["payload"].get("error", ""))
        for e in events_for(api_tokens, key)
    )


# ---- the whole path -------------------------------------------------------------------------


PLAN = {
    "summary": "Two changes at once, then tests for both.",
    "tasks": [
        {
            "ref": "app",
            "title": "Change the app",
            "specification": "Change app.py.",
            "acceptance_criteria": ["app.py changed"],
            "role": "developer",
            "allowed_paths": ["app.py"],
            "budget_usd": 2.0,
            "depends_on": [],
        },
        {
            "ref": "guide",
            "title": "Change the guide",
            "specification": "Change CLAUDE.md.",
            "acceptance_criteria": ["CLAUDE.md changed"],
            "role": "developer",
            "allowed_paths": ["CLAUDE.md"],
            "budget_usd": 3.0,  # it needs a second round for the conflict
            "depends_on": [],
        },
        {
            "ref": "qa",
            "title": "Test both",
            "specification": "Add tests/test_all.py.",
            "acceptance_criteria": ["tests exist"],
            "role": "qa",
            "allowed_paths": ["tests/test_all.py"],
            "budget_usd": 2.0,
            "depends_on": ["app", "guide"],
        },
    ],
    "risks": [],
}


def _review_all(api_url: str, tmp_path: Path) -> None:
    assert reviewer(api_url, tmp_path, lambda: FakeReviewer()).run_once()
    assert reviewer(api_url, tmp_path, lambda: FakeReviewer(), agent="SEC-01").run_once()


def test_from_a_plan_to_every_task_merged(
    api_tokens: Conn, api_url: str, remote: Path, tmp_path: Path
) -> None:
    """Spec, plan, approval, two developers at once, a conflict, QA after, a human merging all."""
    db = api_tokens
    make_project(db, remote, test_command=None)
    set_test_paths(db, HUMAN, "SOC", ["tests/**"])
    parse_plan(PLAN)
    plan_id = int(
        plans.request_plan(
            db, HUMAN, project_key="SOC", specification="Do it all.", budget_usd=Decimal(10)
        )["id"]
    )
    assert plan_worker(api_url, tmp_path, FakePlanner(contract=PLAN)).run_once()
    assert plans.get_plan(db, plan_id)["status"] == "PROPOSED"
    plans.approve_plan(db, HUMAN, plan_id)
    keys = plans.get_plan(db, plan_id)["task_keys"]
    app, guide, qa = keys["app"], keys["guide"], keys["qa"]
    assert get_task(db, qa).state == TaskState.PENDING  # QA waits for both

    # Two developers really run at the same time: each waits inside its run for the other.
    barrier, opener, github = threading.Barrier(2), FakePrOpener(), FakeGitHubState()
    workers = [
        make_worker(
            api_url, tmp_path, EditsItsOwnPath("from a developer\n", barrier), opener, agent=a
        )
        for a in ("DEV-01", "DEV-02")
    ]
    threads = [threading.Thread(target=w.run_once, daemon=True) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert not barrier.broken
    assert get_task(db, app).state == get_task(db, guide).state == TaskState.IN_REVIEW
    assert get_task(db, qa).state == TaskState.PENDING

    # Reviews pass. Both now wait for a human.
    for _ in range(2):
        _review_all(api_url, tmp_path)
    assert get_task(db, app).state == get_task(db, guide).state == TaskState.AWAITING_MERGE
    assert sync_once(db, github) == {app: "nothing to do", guide: "nothing to do"}

    # The human merges the first. Meanwhile someone changes the file the second one touches.
    app_branch = get_task(db, app).branch
    assert app_branch is not None
    _merge_on_remote(remote, tmp_path, app_branch)
    github.merged.add(_number(db, app))
    _commit_to_main(remote, tmp_path, "CLAUDE.md", THEIRS)
    github.conflicted.add(_number(db, guide))
    assert sync_once(db, github) == {app: "merged", guide: "conflict"}
    assert get_task(db, app).state == TaskState.DONE
    assert get_task(db, guide).state == TaskState.READY
    assert get_task(db, qa).state == TaskState.PENDING  # still waiting for the other

    # Rework resolves it on the same PR, and it goes through review again.
    first_number = _number(db, guide)
    resolver = EditsItsOwnPath(RESOLVED)
    assert make_worker(api_url, tmp_path, resolver, opener).run_once()
    assert _on_branch(remote, get_task(db, guide).branch or "", "CLAUDE.md") == RESOLVED.strip()
    assert _number(db, guide) == first_number
    github.conflicted.clear()
    _review_all(api_url, tmp_path)
    assert get_task(db, guide).state == TaskState.AWAITING_MERGE
    guide_branch = get_task(db, guide).branch
    assert guide_branch is not None
    _merge_on_remote(remote, tmp_path, guide_branch)
    github.merged.add(_number(db, guide))
    assert sync_once(db, github) == {guide: "merged"}

    # Only now is QA released, on code that includes both.
    assert get_task(db, qa).state == TaskState.READY
    tester = EditsItsOwnPath("def test_all(): pass\n")
    assert make_worker(
        api_url, tmp_path, tester, opener, agent="QA-01", keep_workspaces=True
    ).run_once()
    assert (tester.requests[0].workspace / "CLAUDE.md").read_text() == RESOLVED
    assert tester.requests[0].role == "qa"
    _review_all(api_url, tmp_path)
    qa_branch = get_task(db, qa).branch
    assert qa_branch is not None
    _merge_on_remote(remote, tmp_path, qa_branch)
    github.merged.add(_number(db, qa))
    sync_once(db, github)

    assert [get_task(db, k).state for k in (app, guide, qa)] == [TaskState.DONE] * 3
    assert _on_branch(remote, "main", "app.py") == "from a developer"
    assert _on_branch(remote, "main", "CLAUDE.md") == RESOLVED.strip()
    assert _on_branch(remote, "main", "tests/test_all.py") == "def test_all(): pass"
    actors = {
        e["actor"] for k in (app, guide, qa) for e in events_for(db, k) if e["type"] == "TASK_DONE"
    }
    assert actors == {"human:github-bharathcmbox"}
    assert check_invariants(db) == []
