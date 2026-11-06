"""Phase 3 T4 LIVE: a real Claude Fable 5.1 code reviewer must catch a planted bug.

Costs real money (budget $1). Run explicitly: make test-live.
"""

import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from axiom.modelproxy.client import ModelProxyClient
from axiom.runtimes.base import Outcome
from axiom.runtimes.review import ReviewRequest
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.workspaces.manager import prepare_review_workspace

pytestmark = pytest.mark.live

ORIGINAL = '''def within_quota(used: int, limit: int) -> bool:
    """Placeholder."""
    raise NotImplementedError
'''
BUGGY = '''def within_quota(used: int, limit: int) -> bool:
    """True while the quota is not exceeded."""
    return used < limit
'''
TESTS = """from quota import within_quota


def test_under_quota():
    assert within_quota(3, 10)


def test_over_quota():
    assert not within_quota(11, 10)
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()  # fmt: skip


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_fable_reviewer_catches_the_planted_off_by_one(
    tmp_path: Path, model_proxy: ModelProxyClient
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "quota.py").write_text(ORIGINAL)
    (src / "CLAUDE.md").write_text("# Toy quota service\nRun tests with `pytest`.\n")
    _git("init", "-q", "-b", "main", cwd=src)
    _git("add", ".", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    _git("checkout", "-qb", "axiom/TOY-002-e1", cwd=src)
    (src / "quota.py").write_text(BUGGY)
    (src / "test_quota.py").write_text(TESTS)
    _git("add", ".", cwd=src)
    _git("commit", "-qm", "Implement within_quota", cwd=src)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)

    ws = prepare_review_workspace(
        tmp_path / "work",
        task_key="TOY-002",
        review_id=1,
        repo_url=bare.as_uri(),
        branch="axiom/TOY-002-e1",
        head_sha=None,
        default_branch="main",
        token=None,
    )
    request = ReviewRequest(
        task_key="TOY-002",
        review_id=1,
        role="code_reviewer",
        title="Implement within_quota",
        specification="Implement within_quota(used, limit): return True while the quota is not "
        "exceeded, i.e. as long as used does not go over limit (using exactly the limit is "
        "allowed). Add tests.",
        acceptance_criteria=[
            "within_quota(10, 10) is True",
            "within_quota(11, 10) is False",
            "tests cover the behaviour",
        ],
        allowed_paths=["quota.py", "test_quota.py"],
        diff=ws.diff,
        developer_result={
            "status": "submitted",
            "summary": "Implemented within_quota; 2 tests pass.",
            "tests_run": {"command": "pytest", "passed": 2, "failed": 0},
        },
        workspace=ws.repo,
        output_dir=ws.out,
        budget_usd=Decimal("1.00"),
        model="claude-fable-5-1",
        effort="high",
        max_turns=20,
    )
    outcome = SandboxRuntime(timeout_seconds=900, model_proxy=model_proxy).run_review(request)
    print("\noutcome:", outcome.outcome, "| cost:", outcome.cost_usd, "| turns:", outcome.turns)
    print("error:", outcome.error)
    if outcome.contract:
        print("verdict:", outcome.contract.verdict, "|", outcome.contract.summary)
        for f in outcome.contract.findings:
            print(f"  [{f.severity}] {f.file}:{f.line} {f.issue} -> {f.required_change}")
    assert outcome.outcome == Outcome.COMPLETED, outcome.error
    assert outcome.contract is not None and outcome.contract.verdict == "FAIL"
    assert any(f.file and "quota.py" in f.file for f in outcome.contract.findings)
    assert outcome.cost_usd is not None and outcome.cost_usd < Decimal("1.00")
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws.repo, capture_output=True, text=True
        ).stdout
        == ""
    )  # the reviewer changed nothing
