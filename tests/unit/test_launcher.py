"""Phase 4 H2: the sandbox launcher (ADR-016), and reading what a sandbox wrote. A fake `docker`
program stands in for the daemon; the real image is exercised by the docker tests."""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.launcher.client import HttpLauncher, LauncherError
from axiom.launcher.runner import LaunchResult, LocalLauncher
from axiom.launcher.server import serve
from axiom.launcher.spec import ContainerSpec, InvalidSpec, Policy, docker_args, validate
from axiom.runtimes.base import Outcome, RunOutcome, RunRequest, read_from_sandbox
from axiom.runtimes.estimate import estimate_from_transcript, with_estimate
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.worker.verify import redact

TOKEN = "launcher-token-for-tests"
FAKE_SECRET = "FAKE-TOKEN-NOT-REAL-0123456789"


def _run_dir(root: Path, name: str = "AX-1-e1") -> tuple[Path, Path]:
    repo, out = root / name / "repo", root / name / "out"
    repo.mkdir(parents=True)
    out.mkdir()
    return repo, out


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "work"
    r.mkdir()
    return r.resolve()


def _spec(repo: Path, out: Path, **kwargs: Any) -> ContainerSpec:
    return ContainerSpec(kwargs.pop("name", "axiom-sbx-ax-1-abcd"), str(repo), str(out), **kwargs)


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    """Records its arguments. `run` prints a line and exits with 3, or hangs if the name says so."""
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        'if [ "$1" = kill ]; then touch "' + str(tmp_path) + '/killed-$2"; exit 0; fi\n'
        'case "$*" in *axiom-sbx-hang*) n=0; while [ $n -lt 400 ]; do '
        '[ -n "$(ls ' + str(tmp_path) + '/killed-* 2>/dev/null)" ] && exit 137; '
        "sleep 0.05; n=$((n+1)); done; exit 1;; esac\n"
        "echo hello from the sandbox\n"
        "exit 3\n"
    )
    script.chmod(0o755)
    return script


# ---- what may be started --------------------------------------------------------------------


def test_a_sound_request_passes(root: Path) -> None:
    repo, out = _run_dir(root)
    env = (
        "ANTHROPIC_API_KEY=run_abc",
        "ANTHROPIC_BASE_URL=http://model-proxy:8080",
        "UV_PYTHON=3.12",
    )
    validate(
        _spec(repo, out, env=env, entrypoint=("sh", "-c", "pytest")), Policy(workspaces_root=root)
    )


@pytest.mark.parametrize(
    "name",
    ["", "postgres", "axiom-api-1", "axiom-sbx-", "axiom-sbx-A", "axiom-sbx-x y", "--privileged"],
)
def test_container_names_must_be_ours(root: Path, name: str) -> None:
    repo, out = _run_dir(root)
    with pytest.raises(InvalidSpec, match="name"):
        validate(_spec(repo, out, name=name), Policy(workspaces_root=root))


@pytest.mark.parametrize(
    "item",
    [
        "AXIOM_GITHUB_TOKEN=x",
        "AXIOM_AGENT_TOKEN=x",
        "PATH=/evil",
        "LD_PRELOAD=/evil.so",
        "HTTPS_PROXY=http://evil",
        "ANTHROPIC_API_KEY=sk-ant-a-real-key",
        "NOT_AN_ASSIGNMENT",
        "UV_PYTHON=3.12\nPATH=/evil",
    ],
)
def test_only_known_harmless_settings_reach_a_sandbox(root: Path, item: str) -> None:
    repo, out = _run_dir(root)
    with pytest.raises(InvalidSpec):
        validate(_spec(repo, out, env=(item,)), Policy(workspaces_root=root))


@pytest.mark.parametrize(
    "entry", [("docker", "ps"), ("sh",), ("sh", "-c"), ("bash", "-c", "x"), ("sh", "-c", "x", "y")]
)
def test_the_only_other_entrypoint_is_a_shell_command(root: Path, entry: tuple[str, ...]) -> None:
    repo, out = _run_dir(root)
    with pytest.raises(InvalidSpec, match="entrypoint"):
        validate(_spec(repo, out, entrypoint=entry), Policy(workspaces_root=root))


@pytest.mark.parametrize(
    ("workspace", "output"),
    [
        ("/", "/tmp/out"),
        ("/etc/repo", "/etc/out"),
        ("/var/run/repo", "/var/run/out"),
        ("{root}/../repo", "{root}/../out"),
        ("{root}/AX-1-e1/repo/../../AX-2-e2/repo", "{root}/AX-2-e2/out"),
        ("relative/repo", "relative/out"),
        ("{root}/AX-1-e1/repo", "{root}/AX-2-e2/out"),  # two different runs
        ("{root}/AX-1-e1/repo", "{root}/AX-1-e1/repo"),
        ("{root}/AX-1-e1", "{root}/AX-1-e1/out"),
        ("{root}/missing/repo", "{root}/missing/out"),
    ],
)
def test_only_a_runs_own_directories_can_be_mounted(
    root: Path, workspace: str, output: str
) -> None:
    _run_dir(root, "AX-1-e1")
    _run_dir(root, "AX-2-e2")
    spec = ContainerSpec("axiom-sbx-ok", workspace.format(root=root), output.format(root=root))
    with pytest.raises(InvalidSpec):
        validate(spec, Policy(workspaces_root=root))


def test_the_workspaces_root_itself_cannot_be_used(root: Path) -> None:
    (root / "repo").mkdir()
    (root / "out").mkdir()
    with pytest.raises(InvalidSpec, match="own directory"):
        validate(_spec(root / "repo", root / "out"), Policy(workspaces_root=root))


def test_a_symlinked_directory_cannot_be_mounted(root: Path, tmp_path: Path) -> None:
    """A way to mount somewhere else that passes a plain prefix check."""
    secret = tmp_path / "elsewhere" / "repo"
    secret.mkdir(parents=True)
    run = root / "AX-1-e1"
    run.mkdir()
    (run / "repo").symlink_to(secret)
    (run / "out").mkdir()
    with pytest.raises(InvalidSpec, match="symlink"):
        validate(_spec(run / "repo", run / "out"), Policy(workspaces_root=root))


def test_a_real_directory_outside_the_root_cannot_be_mounted(root: Path, tmp_path: Path) -> None:
    """Named correctly, and really there: only the root check stands in the way."""
    repo, out = _run_dir(tmp_path / "elsewhere")
    with pytest.raises(InvalidSpec, match="outside the workspaces root"):
        validate(_spec(repo, out), Policy(workspaces_root=root))


def test_a_symlink_higher_up_the_path_cannot_be_used(root: Path, tmp_path: Path) -> None:
    """The last part of the path is a real directory, and the path starts inside the root."""
    (tmp_path / "elsewhere").mkdir()
    repo, _ = _run_dir(tmp_path / "elsewhere")
    link = root / "link"
    link.symlink_to(repo.parent)
    with pytest.raises(InvalidSpec, match="symlink"):
        validate(_spec(link / "repo", link / "out"), Policy(workspaces_root=root))


def test_unknown_fields_are_refused_not_ignored() -> None:
    """A mount, a privilege or an image can't be asked for: there is no field for it."""
    for extra in ("volumes", "privileged", "image", "network", "user", "cap_add"):
        with pytest.raises(InvalidSpec, match="unexpected"):
            ContainerSpec.from_json(
                {"name": "n", "workspace": "w", "output_dir": "o", extra: "anything"}
            )


def test_every_container_has_the_same_restrictions(root: Path) -> None:
    repo, out = _run_dir(root)
    args = docker_args(_spec(repo, out), Policy())
    text = " ".join(args)
    for flag in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit",
    ):
        assert flag in text
    assert "--network axiom_sandbox" in text
    assert "docker.sock" not in text and "--privileged" not in text
    volumes = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert volumes == [f"{repo}:/workspace/repo:rw", f"{out}:/workspace/out:rw"]


# ---- running --------------------------------------------------------------------------------


def _local(fake_docker: Path, root: Path) -> LocalLauncher:
    return LocalLauncher(Policy(docker=str(fake_docker), workspaces_root=root))


def test_output_and_exit_code_come_back_in_the_result(fake_docker: Path, root: Path) -> None:
    repo, out = _run_dir(root)
    result = _local(fake_docker, root).run(_spec(repo, out), 30)
    assert result == LaunchResult(3, "hello from the sandbox\n")
    assert not (out / "sandbox.log").exists()  # nothing to read back from the sandbox's folder


def test_a_refused_request_starts_nothing(fake_docker: Path, root: Path, tmp_path: Path) -> None:
    repo, out = _run_dir(root)
    with pytest.raises(InvalidSpec):
        _local(fake_docker, root).run(_spec(repo, out, name="evil"), 30)
    assert not (tmp_path / "calls.jsonl").exists()


def test_a_timeout_kills_the_container(fake_docker: Path, root: Path, tmp_path: Path) -> None:
    repo, out = _run_dir(root)
    result = _local(fake_docker, root).run(_spec(repo, out, name="axiom-sbx-hang"), 0.2)
    assert result.timed_out and result.exit_code == 124
    assert (tmp_path / "killed-axiom-sbx-hang").exists()


def test_only_containers_it_started_can_be_killed(
    fake_docker: Path, root: Path, tmp_path: Path
) -> None:
    assert _local(fake_docker, root).kill("axiom-db-1") is False
    assert not (tmp_path / "killed-axiom-db-1").exists()


# ---- over HTTP ------------------------------------------------------------------------------


@pytest.fixture
def http(fake_docker: Path, root: Path) -> Iterator[HttpLauncher]:
    server = serve(_local(fake_docker, root), TOKEN, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    yield HttpLauncher(f"http://127.0.0.1:{server.server_address[1]}", TOKEN)
    server.shutdown()
    server.server_close()


def test_a_run_over_http(http: HttpLauncher, root: Path) -> None:
    repo, out = _run_dir(root)
    assert http.run(_spec(repo, out), 30) == LaunchResult(3, "hello from the sandbox\n")


@pytest.mark.parametrize("token", ["", "wrong", "Bearer x"])
def test_it_needs_its_token(http: HttpLauncher, root: Path, token: str, tmp_path: Path) -> None:
    repo, out = _run_dir(root)
    with pytest.raises(LauncherError, match="401"):
        HttpLauncher(http.url, token).run(_spec(repo, out), 30)
    assert not (tmp_path / "calls.jsonl").exists()


def _post(http: HttpLauncher, body: dict[str, Any]) -> int:
    request = urllib.request.Request(
        f"{http.url}/containers/run",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        return urllib.request.urlopen(request, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def test_a_host_mount_cannot_be_requested_over_http(
    http: HttpLauncher, root: Path, tmp_path: Path
) -> None:
    repo, out = _run_dir(root)
    good = {"name": "axiom-sbx-ok", "workspace": str(repo), "output_dir": str(out)}
    attacks = [
        good | {"timeout_seconds": 5, "volumes": ["/:/host"]},
        good | {"timeout_seconds": 5, "privileged": True},
        good | {"timeout_seconds": 5, "workspace": "/"},
        good | {"timeout_seconds": 5, "name": "x --privileged"},
        good | {"timeout_seconds": 10**9},
        good,  # no timeout
    ]
    for attack in attacks:
        assert _post(http, attack) == 422
    assert not (tmp_path / "calls.jsonl").exists()


def test_a_killed_worker_leaves_no_sandbox_behind(
    http: HttpLauncher, root: Path, tmp_path: Path
) -> None:
    """The worker holds the request open. When its connection drops, the launcher kills the container."""
    repo, out = _run_dir(root)
    spec = _spec(repo, out, name="axiom-sbx-hang")
    body = json.dumps(spec.to_json() | {"timeout_seconds": 60}).encode()
    host, port = http.url.removeprefix("http://").split(":")
    conn = socket.create_connection((host, int(port)))
    conn.sendall(
        b"POST /containers/run HTTP/1.1\r\nHost: x\r\n"
        + f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    for _ in range(100):
        if (tmp_path / "calls.jsonl").exists():
            break
        time.sleep(0.05)
    conn.close()  # the worker is gone
    for _ in range(100):
        if (tmp_path / "killed-axiom-sbx-hang").exists():
            break
        time.sleep(0.05)
    assert (tmp_path / "killed-axiom-sbx-hang").exists()


# ---- reading what a sandbox wrote -----------------------------------------------------------


def test_reading_refuses_a_symlink(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text(FAKE_SECRET)
    (tmp_path / "link").symlink_to(secret)
    with pytest.raises(OSError):
        read_from_sandbox(tmp_path / "link", 1000)


def test_reading_refuses_a_fifo_without_hanging(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(OSError, match="regular"):
        read_from_sandbox(tmp_path / "pipe", 1000)


def test_reading_is_limited(tmp_path: Path) -> None:
    (tmp_path / "big").write_text("x" * 100)
    assert read_from_sandbox(tmp_path / "big", 10) == b"x" * 10


def _request(repo: Path, out: Path) -> RunRequest:
    return RunRequest(
        task_key="AX-1",
        title="t",
        specification="s",
        acceptance_criteria=[],
        allowed_paths=[],
        workspace=repo,
        output_dir=out,
        budget_usd=Decimal(1),
    )


@pytest.mark.parametrize("planted", ["sandbox.log", "outcome.json", "transcript.jsonl"])
def test_a_symlink_to_the_environment_reveals_nothing(
    fake_docker: Path, root: Path, tmp_path: Path, planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attack that prompted this: replace a file with a link to /proc/self/environ, exit
    without an outcome, and let the harness copy the worker's tokens into the error."""
    repo, out = _run_dir(root)
    secret = tmp_path / "environ"  # stands in for /proc/self/environ, with a known value
    secret.write_text(f"AXIOM_GITHUB_TOKEN={FAKE_SECRET}\n")
    (out / planted).symlink_to(secret)
    runtime = SandboxRuntime(docker=str(fake_docker), launcher=_local(fake_docker, root))
    outcome = with_estimate(runtime.run(_request(repo, out)), out)
    assert outcome.outcome == Outcome.FAILED
    assert FAKE_SECRET not in (outcome.error or "")
    assert FAKE_SECRET not in json.dumps(outcome.cost_payload())


def test_a_symlinked_transcript_gives_no_estimate(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text('{"type": "ResultMessage", "data": {"total_cost_usd": 1.0}}\n')
    (tmp_path / "transcript.jsonl").symlink_to(secret)
    assert estimate_from_transcript(tmp_path / "transcript.jsonl") is None


def test_an_outcome_that_is_not_json_is_a_failure_not_a_crash(
    fake_docker: Path, root: Path
) -> None:
    repo, out = _run_dir(root)
    (out / "outcome.json").write_text("not json")
    outcome = SandboxRuntime(launcher=_local(fake_docker, root)).run(_request(repo, out))
    assert isinstance(outcome, RunOutcome) and outcome.outcome == Outcome.FAILED
    assert "not valid JSON" in (outcome.error or "")


def test_the_log_is_kept_where_the_sandbox_cannot_reach_it(fake_docker: Path, root: Path) -> None:
    repo, out = _run_dir(root)
    SandboxRuntime(launcher=_local(fake_docker, root)).run(_request(repo, out))
    assert (repo.parent / "sandbox.log").read_text() == "hello from the sandbox\n"
    assert not (out / "sandbox.log").exists()


def test_an_unreachable_launcher_fails_the_run(root: Path) -> None:
    repo, out = _run_dir(root)
    runtime = SandboxRuntime(launcher=HttpLauncher("http://127.0.0.1:9", TOKEN))
    outcome = runtime.run(_request(repo, out))
    assert outcome.outcome == Outcome.FAILED and "could not start" in (outcome.error or "")
    assert runtime.exec(repo, out, "true", 5).exit_code == 1


# ---- errors are cleaned before they're stored -----------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-abcdefghijklmnopqrstuvwxyz",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "axm_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_credentials_are_removed_from_errors(secret: str) -> None:
    assert secret not in redact(f"failed: KEY={secret} end")
    assert "failed:" in redact(f"failed: KEY={secret} end")


def test_this_processs_own_secrets_are_removed_whatever_their_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_MODEL_PROXY_ADMIN_TOKEN", "abcdef0123456789abcdef")
    assert redact("x abcdef0123456789abcdef y") == "x [REDACTED AXIOM_MODEL_PROXY_ADMIN_TOKEN] y"
