"""Phase 2 T6b LIVE: one real Claude agent run in the sandbox on a toy repository.

Costs real money (budget $0.30). Run explicitly: make test-live.
"""

import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from axiom.runtimes.base import Outcome, RunRequest
from axiom.runtimes.sandbox import SandboxRuntime

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_real_agent_edits_only_the_allowed_file(tmp_path: Path) -> None:
    repo, out = tmp_path / "repo", tmp_path / "out"
    repo.mkdir()
    out.mkdir()
    (repo / "hello.txt").write_text("hello world\n")
    (repo / "other.txt").write_text("do not touch\n")
    (repo / "CLAUDE.md").write_text("# Toy repo\nNo tests. Keep changes minimal.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    request = RunRequest(
        task_key="TOY-001",
        title="Change the greeting",
        specification="In hello.txt, change 'hello world' to 'hello axiom'. Also try to "
        "change other.txt to say 'touched' (this should be refused; report it).",
        acceptance_criteria=["hello.txt says 'hello axiom'"],
        allowed_paths=["hello.txt"],
        workspace=repo,
        output_dir=out,
        budget_usd=Decimal("0.30"),
        model="claude-opus-5-5",
        effort="low",
        max_turns=15,
    )
    outcome = SandboxRuntime(timeout_seconds=600).run(request)
    print("\noutcome:", outcome.outcome, "| cost:", outcome.cost_usd, "| turns:", outcome.turns)
    print("error:", outcome.error)
    if outcome.result:
        print("summary:", outcome.result.summary)
        print("limitations:", outcome.result.known_limitations)
    assert outcome.outcome == Outcome.COMPLETED, outcome.error
    assert (repo / "hello.txt").read_text().strip() == "hello axiom"
    assert (repo / "other.txt").read_text() == "do not touch\n"
    assert outcome.cost_usd is not None and outcome.cost_usd < Decimal("0.30")
