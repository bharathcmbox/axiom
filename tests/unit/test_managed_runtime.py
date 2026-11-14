"""Phase 5 T6: the Managed Agents review runtime, without a network (ADR-020)."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from axiom.contracts import REVIEW_CHECKLISTS
from axiom.runtimes.base import Outcome
from axiom.runtimes.managed import (
    ENVIRONMENT_CONFIG,
    READ_TOOLS,
    SUBMIT_TOOL,
    ManagedReviewRuntime,
    SessionResult,
    agent_config,
    cents_to_usd,
    drive,
    inline_refs,
    outcome_of,
    repo_files,
)
from axiom.runtimes.review import ReviewRequest


def _review(role: str = "code_reviewer") -> dict[str, Any]:
    return {"verdict": "PASS", "summary": "fine", "findings": [],
            "checklist": {i: {"result": "ok", "note": "read"} for i in REVIEW_CHECKLISTS[role]}}  # fmt: skip


def _idle(reason: str) -> NS:
    return NS(type="session.status_idle", stop_reason=NS(type=reason))


def test_the_agent_can_only_read_and_submit() -> None:
    config = agent_config("code_reviewer", "claude-fable-5-2")
    toolset, submit = config["tools"]
    enabled = {c["name"] for c in toolset["configs"] if c["enabled"]}
    assert enabled == set(READ_TOOLS)
    assert toolset["default_config"]["enabled"] is False
    assert submit["type"] == "custom" and submit["name"] == SUBMIT_TOOL
    assert "$defs" not in json.dumps(submit["input_schema"])
    required = submit["input_schema"]["properties"]["checklist"]["required"]
    assert required == list(REVIEW_CHECKLISTS["code_reviewer"])


def test_the_sandbox_has_no_way_out() -> None:
    net = ENVIRONMENT_CONFIG["networking"]
    assert net == {"type": "limited", "allow_package_managers": False,
                   "allow_mcp_servers": False, "allowed_hosts": []}  # fmt: skip


def test_refs_are_inlined() -> None:
    schema = {"$defs": {"A": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/A"}}}
    assert inline_refs(schema) == {"properties": {"a": {"type": "string"}}}


def test_a_submitted_review_is_validated_like_any_other() -> None:
    sent: list[Any] = []
    events = [NS(type="agent.tool_use", id="e1", evaluated_permission="allow"),
              NS(type="agent.custom_tool_use", id="e2", name=SUBMIT_TOOL, input=_review()),
              _idle("requires_action"), _idle("end_turn")]  # fmt: skip
    result = drive(events, sent.extend, deadline=float("inf"))
    assert sent[0]["type"] == "user.custom_tool_result" and sent[0]["custom_tool_use_id"] == "e2"
    outcome = outcome_of(result, "code_reviewer", Decimal("0.31"), (10, 5), None)
    assert outcome.outcome == Outcome.COMPLETED and outcome.contract is not None
    assert outcome.cost_usd == Decimal("0.31")


def test_an_invalid_review_fails_the_run() -> None:
    bad = _review() | {"checklist": {}}
    result = drive([NS(type="agent.custom_tool_use", id="e", name=SUBMIT_TOOL, input=bad),
                    _idle("end_turn")], lambda e: None, deadline=float("inf"))  # fmt: skip
    outcome = outcome_of(result, "code_reviewer", None, (None, None), None)
    assert outcome.outcome == Outcome.FAILED and "not examined" in (outcome.error or "")


def test_a_call_waiting_for_approval_is_denied() -> None:
    sent: list[Any] = []
    drive([NS(type="agent.tool_use", id="e9", evaluated_permission="ask"), _idle("end_turn")],
          sent.extend, deadline=float("inf"))  # fmt: skip
    assert sent == [{"type": "user.tool_confirmation", "tool_use_id": "e9", "result": "deny",
                     "deny_message": "No approvals in a review."}]  # fmt: skip


def test_only_one_review_is_taken() -> None:
    sent: list[Any] = []
    events = [NS(type="agent.custom_tool_use", id="a", name=SUBMIT_TOOL, input=_review()),
              NS(type="agent.custom_tool_use", id="b", name=SUBMIT_TOOL, input={"x": 1}),
              _idle("end_turn")]  # fmt: skip
    result = drive(events, sent.extend, deadline=float("inf"))
    assert result.contract_input == _review() and "only once" in json.dumps(sent[1])


def test_the_budget_and_the_deadline_end_a_session() -> None:
    at_cap = drive([_idle("budget_reached")], lambda e: None, deadline=float("inf"))
    assert outcome_of(at_cap, "code_reviewer", None, (None, None), None).outcome == (
        Outcome.BUDGET_EXCEEDED)  # fmt: skip
    sent: list[Any] = []
    late = drive([NS(type="agent.message")], sent.extend, deadline=0, clock=lambda: 1)
    assert late.timed_out and sent == [{"type": "user.interrupt"}]
    assert outcome_of(late, "code_reviewer", None, (None, None), None).error == "review timed out"


def test_nothing_submitted_is_a_failure() -> None:
    outcome = outcome_of(SessionResult(), "security_reviewer", None, (None, None), None)
    assert outcome.outcome == Outcome.FAILED and outcome.error == "no review submitted"


def test_list_cost_is_in_cents() -> None:
    assert cents_to_usd("31") == Decimal("0.3100") and cents_to_usd(None) is None


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    (repo / "big.bin").write_bytes(b"0" * (600 * 1024))
    (repo / "untracked.txt").write_text("no\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "a.py", "big.bin"], cwd=repo, check=True)
    return repo


def test_only_tracked_files_of_reasonable_size_are_uploaded(tmp_path: Path) -> None:
    assert repo_files(_repo(tmp_path)) == [Path("a.py")]


class FakeClient:
    """Just enough of the SDK's shape to run a whole review."""

    def __init__(self, events: list[Any]) -> None:
        self.created: dict[str, Any] = {}
        self.sent: list[Any] = []
        events_ns = NS(
            stream=self._stream, send=lambda session_id, events: self.sent.extend(events)
        )
        self.beta = NS(
            environments=NS(create=lambda **kw: self._made("environment", kw, "env_1")),
            agents=NS(create=lambda **kw: self._made("agent", kw, "agent_1")),
            sessions=NS(create=lambda **kw: self._made("session", kw, "sesn_1"), events=events_ns,
                        retrieve=lambda session_id: NS(status="idle", usage=NS(
                            list_cost=NS(amount="42"), input_tokens=900, output_tokens=80))),
        )  # fmt: skip
        self.files = NS(upload=lambda file: NS(id=f"file_{file[0]}"))
        self._events = events

    def _made(self, kind: str, kw: dict[str, Any], ident: str) -> NS:
        self.created[kind] = kw
        return NS(id=ident)

    def _stream(self, session_id: str) -> Any:
        events = self._events

        class Stream:
            def __enter__(self) -> list[Any]:
                return events

            def __exit__(self, *a: object) -> None: ...

        return Stream()


def test_a_whole_review_through_the_adapter(tmp_path: Path) -> None:
    client = FakeClient([NS(type="agent.custom_tool_use", id="e", name=SUBMIT_TOOL,
                            input=_review()), _idle("end_turn")])  # fmt: skip
    runtime = ManagedReviewRuntime(client, ids_file=tmp_path / "ids.json")
    request = ReviewRequest(task_key="SOC-1", review_id=0, role="code_reviewer", title="t",
                            specification="s", acceptance_criteria=["c"], allowed_paths=["a.py"],
                            diff="+x", developer_result=None, workspace=_repo(tmp_path),
                            output_dir=tmp_path / "out", budget_usd=Decimal("1.50"),
                            model="claude-fable-5-2")  # fmt: skip
    outcome = runtime.run_review(request)
    assert outcome.outcome == Outcome.COMPLETED and outcome.cost_usd == Decimal("0.42")
    session = client.created["session"]
    assert session["budget"]["max_list_cost"] == {"amount": "150", "currency": "USD"}
    assert session["resources"] == [{"type": "file", "file_id": "file_a.py",
                                     "mount_path": "/repo/a.py"}]  # fmt: skip
    assert client.sent[0]["type"] == "user.message"  # the prompt, sent once the stream is open
    assert json.loads((tmp_path / "ids.json").read_text()) == {
        "agent:code_reviewer:claude-fable-5-2": "agent_1", "environment": "env_1"}  # fmt: skip
    ManagedReviewRuntime(client, ids_file=tmp_path / "ids.json").run_review(request)
    assert client.created["agent"]["model"] == "claude-fable-5-2"  # reused, not created again


@pytest.mark.parametrize("role", sorted(REVIEW_CHECKLISTS))
def test_each_role_gets_its_own_checklist_schema(role: str) -> None:
    schema = agent_config(role, "m")["tools"][1]["input_schema"]
    assert set(schema["properties"]["checklist"]["required"]) == set(REVIEW_CHECKLISTS[role])


def test_an_invalid_submission_goes_back_with_its_problems_then_a_valid_one_counts() -> None:
    sent: list[Any] = []
    flattened = {"verdict": "PASS", "summary": "ok", "checklist": "all ok", "findings": "none"}
    events = [NS(type="agent.custom_tool_use", id="a", name=SUBMIT_TOOL, input=flattened),
              NS(type="agent.custom_tool_use", id="b", name=SUBMIT_TOOL, input=_review()),
              _idle("end_turn")]  # fmt: skip
    from axiom.runtimes.managed import _problem

    result = drive(events, sent.extend, deadline=float("inf"),
                   validate=lambda c: _problem(c, "code_reviewer"))  # fmt: skip
    assert "That review is invalid" in json.dumps(sent[0]) and len(result.rejected) == 1
    assert result.contract_input == _review()
    assert outcome_of(result, "code_reviewer", None, (None, None), None).outcome == (
        Outcome.COMPLETED)  # fmt: skip


def test_three_invalid_submissions_fail_the_run() -> None:
    from axiom.runtimes.managed import MAX_SUBMISSIONS, _problem

    bad = {"verdict": "PASS"}
    events = [NS(type="agent.custom_tool_use", id=str(i), name=SUBMIT_TOOL, input=bad)
              for i in range(MAX_SUBMISSIONS)] + [_idle("end_turn")]  # fmt: skip
    result = drive(events, lambda e: None, deadline=float("inf"),
                   validate=lambda c: _problem(c, "code_reviewer"))  # fmt: skip
    assert len(result.rejected) == MAX_SUBMISSIONS - 1
    assert outcome_of(result, "code_reviewer", None, (None, None), None).outcome == Outcome.FAILED
