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
from axiom.runtimes.sandbox import SandboxRuntime

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
    assert lines["anthropic via proxy"].startswith("HTTP")
    assert lines["pypi via proxy"].startswith("HTTP")
    assert "403 Filtered" in lines["github via proxy"]
    assert "403 Filtered" in lines["example via proxy"]
    assert lines["anthropic no proxy"].startswith("BLOCKED")
    assert lines["raw tcp 1.1.1.1 port 443"].startswith("BLOCKED")


def test_sandbox_receives_no_credentials_but_the_anthropic_key(
    request_: RunRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness's environment holds every secret; only the Anthropic key may pass."""
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
    assert "ANTHROPIC_API_KEY=sk-ant-test-dummy" in env_dump
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
