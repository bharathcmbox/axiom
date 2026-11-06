"""Phase 2 T4: FakeRuntime scenarios behave like a (mis)behaving agent, without any cost."""

import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from axiom.runtimes.base import Outcome, RunRequest, Runtime
from axiom.runtimes.fake import FakeRuntime, Scenario, load_scripted_developer


def _request(tmp_path: Path) -> RunRequest:
    ws = tmp_path / "repo"
    ws.mkdir(parents=True)
    return RunRequest(
        task_key="AX-001",
        title="Add pytest",
        specification="s",
        acceptance_criteria=["tests run"],
        allowed_paths=["agent/pyproject.toml", "CLAUDE.md"],
        workspace=ws,
        output_dir=tmp_path / "out",
        budget_usd=Decimal("2.40"),
    )


def test_fake_runtime_satisfies_the_protocol() -> None:
    runtime: Runtime = FakeRuntime()
    assert runtime.name == "fake"


def test_success_edits_an_allowed_file_and_reports(tmp_path: Path) -> None:
    req = _request(tmp_path)
    events: list[dict[str, object]] = []
    out = FakeRuntime().run(req, on_event=events.append)
    assert out.outcome == Outcome.COMPLETED and out.result is not None
    assert out.result.files_changed == ["agent/pyproject.toml"]
    assert (req.workspace / "agent/pyproject.toml").exists()
    assert out.cost_usd == Decimal("0.42") and out.transcript_path is not None
    assert out.transcript_path.exists()
    assert [e["type"] for e in events] == ["started", "edit"]
    assert out.cost_payload() == {"usd": "0.42", "input_tokens": 1200, "output_tokens": 300}


def test_custom_edits(tmp_path: Path) -> None:
    req = _request(tmp_path)
    out = FakeRuntime(edits={"CLAUDE.md": "# updated\n"}).run(req)
    assert (req.workspace / "CLAUDE.md").read_text() == "# updated\n"
    assert out.result is not None and out.result.files_changed == ["CLAUDE.md"]


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        (Scenario.FAIL, Outcome.FAILED),
        (Scenario.BUDGET_EXCEEDED, Outcome.BUDGET_EXCEEDED),
        (Scenario.NEEDS_INPUT, Outcome.NEEDS_INPUT),
        (Scenario.MALFORMED_RESULT, Outcome.FAILED),
        (Scenario.EDIT_OUTSIDE_ALLOWED, Outcome.COMPLETED),
        (Scenario.LEAK_SECRET, Outcome.COMPLETED),
    ],
)
def test_scenarios(tmp_path: Path, scenario: Scenario, outcome: Outcome) -> None:
    out = FakeRuntime(scenario=scenario).run(_request(tmp_path))
    assert out.outcome == outcome


def test_misbehaving_scenarios_leave_evidence_for_the_harness(tmp_path: Path) -> None:
    req = _request(tmp_path)
    FakeRuntime(scenario=Scenario.EDIT_OUTSIDE_ALLOWED).run(req)
    assert (req.workspace / "NOT_ALLOWED.txt").exists()
    req2 = _request(tmp_path / "second")
    FakeRuntime(scenario=Scenario.LEAK_SECRET).run(req2)
    assert "sk-ant-api03-" in (req2.workspace / "agent/pyproject.toml").read_text()


def test_malformed_result_reports_why(tmp_path: Path) -> None:
    out = FakeRuntime(scenario=Scenario.MALFORMED_RESULT).run(_request(tmp_path))
    assert out.error is not None and out.error.startswith("invalid result:")


def test_budget_exceeded_reports_spend_above_budget(tmp_path: Path) -> None:
    out = FakeRuntime(scenario=Scenario.BUDGET_EXCEEDED).run(_request(tmp_path))
    assert out.cost_usd == Decimal("2.41")


def test_hang_until_cancelled(tmp_path: Path) -> None:
    runtime = FakeRuntime(scenario=Scenario.HANG)
    result: list[Outcome] = []
    t = threading.Thread(target=lambda: result.append(runtime.run(_request(tmp_path)).outcome))
    t.start()
    runtime.cancel()
    t.join(timeout=5)
    assert result == [Outcome.CANCELLED]


def test_scripted_developer_applies_a_patch_and_returns_its_report_verbatim(tmp_path: Path) -> None:
    report = {
        "status": "submitted",
        "summary": "Added the queue",
        "files_changed": ["CLAUDE.md"],
        "criteria": [{"criterion": "c", "met": True, "evidence": "test_queue passes"}],
    }
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"edits": {"CLAUDE.md": "# new\n"}, "result": report}))
    req = _request(tmp_path)
    out = load_scripted_developer(patch).run(req)
    assert (req.workspace / "CLAUDE.md").read_text() == "# new\n"
    assert out.outcome == Outcome.COMPLETED and out.result is not None
    assert out.result.model_dump(exclude_defaults=True) == report
    assert "fake" not in json.dumps(out.result.model_dump())  # nothing tips off a reviewer
    assert out.cost_usd == Decimal("0")  # a prepared patch costs nothing
