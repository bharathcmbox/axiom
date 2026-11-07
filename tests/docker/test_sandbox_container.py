"""Phase 2 T6b: the sandbox container's isolation, using the real image (make test-docker).

No API calls: the agent is the FakeRuntime inside the container (AXIOM_FAKE_SCENARIO).
"""

import os
import shutil
import subprocess
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from axiom.runtimes.base import Outcome, RunRequest
from axiom.runtimes.plan import PlanRequest
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.worker.verify import check_changes, commit
from axiom.workspaces.manager import prepare_workspace

pytestmark = pytest.mark.docker
IMAGE = "axiom-sandbox:latest"


def _have_image() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


if not _have_image():
    pytest.skip("axiom-sandbox image not built (make sandbox-image)", allow_module_level=True)


@pytest.fixture
def request_(tmp_path: Path) -> RunRequest:
    repo, out = tmp_path / "repo", tmp_path / "out"
    repo.mkdir()
    out.mkdir()
    (repo / "CLAUDE.md").write_text("# guide\n")
    return RunRequest(
        task_key="SOC-001", title="t", specification="s", acceptance_criteria=["c"],
        allowed_paths=["CLAUDE.md"], workspace=repo, output_dir=out,
        budget_usd=Decimal("1.00"),
    )  # fmt: skip


def _run_in_sandbox(req: RunRequest, *cmd: str) -> str:
    """Start the sandbox exactly as SandboxRuntime would, with a different entrypoint."""
    args = SandboxRuntime().docker_args(req, f"axiom-test-{os.getpid()}", entrypoint=list(cmd))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return proc.stdout + proc.stderr


def test_egress_is_limited_to_the_allowlist(request_: RunRequest) -> None:
    probe = Path(__file__).with_name("egress_probe.py")
    shutil.copy(probe, request_.output_dir / "probe.py")
    out = _run_in_sandbox(request_, "python", "/workspace/out/probe.py")
    pairs = (line.split(" = ", 1) for line in out.strip().splitlines() if " = " in line)
    lines = {k.strip(): v.strip() for k, v in pairs}
    assert "403 Filtered" in lines["anthropic via proxy"]  # only the model proxy reaches it
    assert lines["pypi via proxy"].startswith("HTTP")
    assert "403 Filtered" in lines["github via proxy"]
    assert "403 Filtered" in lines["example via proxy"]
    assert lines["anthropic no proxy"].startswith("BLOCKED")
    assert lines["raw tcp 1.1.1.1 port 443"].startswith("BLOCKED")


def test_sandbox_receives_no_credentials_at_all(
    request_: RunRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the harness's environment holds, none of it passes: the agent's model access is a
    run token from the proxy (ADR-015)."""
    secrets_in_harness = {
        "AXIOM_GITHUB_TOKEN": "github_pat_SHOULD_NOT_LEAK",
        "AXIOM_AGENT_TOKEN": "axm_SHOULD_NOT_LEAK",
        "GH_TOKEN": "gho_SHOULD_NOT_LEAK",
        "AXIOM_DATABASE_URL": "postgresql://axiom:axiom@db/axiom",
        "ANTHROPIC_API_KEY": "sk-ant-test-dummy",
    }
    for name, value in secrets_in_harness.items():
        monkeypatch.setenv(name, value)
    env_dump = _run_in_sandbox(request_, "env")
    assert "SHOULD_NOT_LEAK" not in env_dump
    assert "AXIOM_DATABASE_URL" not in env_dump
    assert "sk-ant-test-dummy" not in env_dump and "ANTHROPIC_API_KEY" not in env_dump
    assert "HOME=/home/agent" in env_dump
    assert "HTTPS_PROXY=http://egress-proxy:3128" in env_dump


def test_sandbox_runs_as_the_workspace_owner_with_a_read_only_root(request_: RunRequest) -> None:
    uid = str(request_.workspace.stat().st_uid)
    out = _run_in_sandbox(
        request_,
        "sh",
        "-c",
        "id -u; touch /opt/x 2>&1 || echo ROOT_RO; touch /workspace/repo/ok && echo WS_RW; "
        "ls -A /home/agent | wc -l",
    )
    lines = out.split()
    assert lines[0] == uid
    assert "ROOT_RO" in out and "WS_RW" in out
    assert lines[-1] == "0"  # empty HOME: no user logins or settings
    (request_.workspace / "ok").unlink()


def test_fake_agent_run_end_to_end(request_: RunRequest) -> None:
    outcome = SandboxRuntime(fake_scenario="success").run(request_)
    assert outcome.outcome == Outcome.COMPLETED, outcome.error
    assert outcome.result is not None and outcome.result.files_changed == ["CLAUDE.md"]
    edited = request_.workspace / "CLAUDE.md"
    assert edited.read_text().startswith("# change for SOC-001")
    assert edited.stat().st_uid == request_.workspace.stat().st_uid
    assert outcome.transcript_path is not None and outcome.transcript_path.exists()


def test_cancel_kills_the_container(request_: RunRequest) -> None:
    rt = SandboxRuntime(fake_scenario="hang")
    result: list[Outcome] = []
    t = threading.Thread(target=lambda: result.append(rt.run(request_).outcome))
    t.start()
    for _ in range(100):
        if (
            rt._container
            and subprocess.run(["docker", "inspect", rt._container], capture_output=True).returncode
            == 0
        ):
            break
        threading.Event().wait(0.2)
    rt.cancel()
    t.join(timeout=60)
    assert result == [Outcome.CANCELLED]


def test_timeout_fails_the_run(request_: RunRequest) -> None:
    outcome = SandboxRuntime(fake_scenario="hang", timeout_seconds=5).run(request_)
    assert outcome.outcome == Outcome.FAILED and "timed out" in (outcome.error or "")


def test_exec_runs_commands_without_any_credentials(
    request_: RunRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    rt = SandboxRuntime()
    ok = rt.exec(request_.workspace, request_.output_dir, "env; echo marker; exit 3", 120)
    assert ok.exit_code == 3 and "marker" in ok.output
    assert "ANTHROPIC_API_KEY" not in ok.output  # verification needs no API key
    slow = rt.exec(request_.workspace, request_.output_dir, "sleep 30", 3)
    assert slow.exit_code == 124 and "timed out" in slow.output


def test_review_run_in_the_container(request_: RunRequest) -> None:
    from axiom.runtimes.review import ReviewRequest

    review = ReviewRequest(
        task_key="SOC-001", review_id=3, role="security_reviewer", title="t", specification="s",
        acceptance_criteria=[], allowed_paths=["CLAUDE.md"], diff="+x\n", developer_result=None,
        workspace=request_.workspace, output_dir=request_.output_dir, budget_usd=Decimal("1.00"),
    )  # fmt: skip
    outcome = SandboxRuntime(fake_review="fail").run_review(review)
    assert outcome.outcome == Outcome.COMPLETED, outcome.error
    assert outcome.contract is not None and outcome.contract.verdict == "FAIL"
    assert outcome.contract.findings[0].issue.startswith("security_reviewer")


def test_uv_with_can_run_tools_from_its_cache(request_: RunRequest) -> None:
    """Regression: HOME is a tmpfs, noexec by default, which broke `uv run --with pytest` (the
    verified test run) with 'Permission denied' unless the project itself depended on pytest."""
    result = SandboxRuntime().exec(
        request_.workspace,
        request_.output_dir,
        "uv run --no-project --with pytest pytest --version",
        300,
    )
    assert result.exit_code == 0, result.output
    assert "pytest" in result.output


@pytest.mark.parametrize("version", ["3.12", "3.13"])
def test_uv_can_use_the_projects_python(request_: RunRequest, version: str) -> None:
    """Installed in the image: the sandbox has a read-only root and may not download one."""
    rt = SandboxRuntime()
    command = f"export UV_PYTHON={version}; cd /tmp && uv run --no-project python --version"
    done = rt.exec(request_.workspace, request_.output_dir, command, 120)
    assert done.exit_code == 0 and f"Python {version}" in done.output, done.output


def test_an_attack_planted_from_the_real_sandbox_does_not_reach_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assume the model is compromised: it plants, from inside the real sandbox, what git would
    run afterwards in the harness (ADR-014). Fake credentials; the harness then does its real work."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE-TOKEN-NOT-REAL")
    bare = tmp_path / "remote.git"
    src = tmp_path / "src"
    src.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b"],
    ):
        subprocess.run(["git", *args], cwd=src, check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    ws = prepare_workspace(tmp_path / "work", task_key="AX-1", execution_id=1,
                           repo_url=str(bare), default_branch="main", token=None)  # fmt: skip
    rt = SandboxRuntime()
    plant = (
        "cd /workspace/repo && ls /workspace > /workspace/out/mounts.txt"
        " && printf '#!/bin/sh\\nenv > /tmp/loot-from-harness\\ncat\\n' > .git/steal.sh"
        " && chmod +x .git/steal.sh"
        " && git config --file .git/config core.fsmonitor .git/steal.sh"
        " && git config --file .git/config filter.x.clean .git/steal.sh"
        " && echo '*.md filter=x' > .gitattributes && echo changed > a.md"
    )
    planted = rt.exec(ws.repo, ws.out, plant, 120)
    assert planted.exit_code == 0, planted.output
    assert (ws.out / "mounts.txt").read_text().split() == ["out", "repo"]  # no harness.git in reach
    assert "fsmonitor" in (ws.repo / ".git" / "config").read_text()
    loot = Path("/tmp/loot-from-harness")
    loot.unlink(missing_ok=True)
    check = check_changes(ws.repo, ws.base_sha, ["**"])
    assert check.ok, check.problems
    commit(ws.repo, "work", "A <a@b>")
    assert not loot.exists()


def test_plan_run_in_the_container(request_: RunRequest) -> None:
    plan = PlanRequest(
        plan_id=7,
        project_key="SOC",
        specification="s",
        plan_budget_usd=Decimal(5),
        test_paths=["tests/**"],
        workspace=request_.workspace,
        output_dir=request_.output_dir,
        budget_usd=Decimal(1),
    )
    outcome = SandboxRuntime(fake_plan="good").run_plan(plan)
    assert outcome.outcome == Outcome.COMPLETED and outcome.contract is not None
    assert outcome.contract["tasks"][0]["ref"] == "impl"
