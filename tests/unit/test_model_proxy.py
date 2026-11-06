"""Phase 4 H1: the model proxy (ADR-015). A real HTTP proxy against a fake Anthropic, no spend."""

import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axiom.modelproxy.client import ModelProxyClient, ModelProxyError
from axiom.modelproxy.runs import RETENTION_SECONDS, Refused, Runs
from axiom.modelproxy.server import SseMeter, serve
from axiom.runtimes.base import Outcome, RunRequest
from axiom.runtimes.review import ReviewRequest
from axiom.runtimes.sandbox import MODEL_RUN_FILE, SandboxRuntime
from axiom.sandbox.runner import to_outcome
from axiom.worker.recover_costs import recovered_cost

REAL_KEY = "sk-ant-THE-REAL-KEY-abcdefghijklmnopqrstuvwxyz"
ADMIN = "admin-token-for-tests"
OPUS = "claude-opus-5-5"
USAGE_START = {
    "input_tokens": 2,
    "cache_creation_input_tokens": 1000,
    "cache_read_input_tokens": 5000,
    "output_tokens": 3,
}
SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"m","usage":'
    + json.dumps(USAGE_START).encode()
    + b"}}\n\n"
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","delta":{"text":"hello"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","usage":{"output_tokens":100}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
# 2 x $4 + 1000 x $5 + 5000 x $0.20 + 100 x $20, per million
SSE_COST = Decimal("0.008008")


@dataclass
class Upstream:
    """Stands in for Anthropic. Records exactly what the proxy sent."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    chunk_size: int = 65536
    body: bytes = SSE
    content_type: str = "text/event-stream"
    gate: threading.Event | None = None  # when set: send the first chunk, wait, send the rest
    cut_after: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def upstream() -> Iterator[tuple[Upstream, str]]:
    state = Upstream()

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            state.requests.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": body}
            )
            self.send_response(state.status)
            self.send_header("content-type", state.content_type)
            for name, value in state.extra_headers.items():
                self.send_header(name, value)
            self.end_headers()
            data = state.body[: state.cut_after] if state.cut_after else state.body
            for i in range(0, len(data), state.chunk_size):
                self.wfile.write(data[i : i + state.chunk_size])
                self.wfile.flush()
                if state.gate is not None and i == 0:
                    state.gate.wait(10)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Rig:
    upstream: Upstream
    url: str
    runs: Runs
    clock: Clock
    client: ModelProxyClient

    def ask(
        self,
        token: str | None,
        model: str = OPUS,
        path: str = "/v1/messages",
        method: str = "POST",
    ) -> tuple[int, bytes, dict[str, str]]:
        headers = {"content-type": "application/json", "authorization": "Bearer should-be-dropped"}
        if token is not None:
            headers["x-api-key"] = token
        else:
            del headers["authorization"]
        data = json.dumps({"model": model, "stream": True}).encode() if method == "POST" else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, reply.read(), dict(reply.headers.items())
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers.items())


@pytest.fixture
def rig(upstream: tuple[Upstream, str]) -> Iterator[Rig]:
    state, upstream_url = upstream
    clock = Clock()
    runs = Runs(clock=clock)
    server = serve(runs, REAL_KEY, ADMIN, upstream_url, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield Rig(state, url, runs, clock, ModelProxyClient(url, ADMIN))
    server.shutdown()
    server.server_close()


def _open(rig: Rig, budget: str = "1.00", model: str | None = OPUS, ttl: float = 120) -> Any:
    return rig.client.open(Decimal(budget), model, ttl)


# ---- the key --------------------------------------------------------------------------------


def test_the_real_key_is_swapped_in_and_the_run_token_stays_behind(rig: Rig) -> None:
    grant = _open(rig)
    status, _, _ = rig.ask(grant.token, path="/v1/messages?beta=true")
    assert status == 200
    sent = rig.upstream.requests[0]
    headers = {k.lower(): v for k, v in sent["headers"].items()}
    assert headers["x-api-key"] == REAL_KEY and "authorization" not in headers
    assert grant.token not in json.dumps(sent["headers"])
    assert sent["path"] == "/v1/messages?beta=true"  # the CLI's query survives
    assert json.loads(sent["body"]) == {"model": OPUS, "stream": True}


def test_the_real_key_never_comes_back_or_reaches_the_log(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    grant = _open(rig)
    replies = [
        rig.ask(grant.token),
        rig.ask("wrong"),
        rig.ask(grant.token, model="other"),
        rig.ask(grant.token, path="/v1/models", method="GET"),
    ]
    for status, body, headers in replies:
        assert status in (200, 401, 403)
        assert REAL_KEY.encode() not in body and REAL_KEY not in json.dumps(headers)
    assert REAL_KEY not in caplog.text and grant.token not in caplog.text


# ---- refusals: every one of these must never reach Anthropic --------------------------------


def _assert_refused(rig: Rig, reply: tuple[int, bytes, dict[str, str]], status: int) -> None:
    assert reply[0] == status
    assert json.loads(reply[1])["type"] == "error"  # Anthropic's shape, so the CLI reports it
    assert rig.upstream.requests == []


@pytest.mark.parametrize("token", [None, "", "guess", REAL_KEY, ADMIN])
def test_a_request_without_a_live_run_token_is_refused(rig: Rig, token: str | None) -> None:
    _open(rig)
    _assert_refused(rig, rig.ask(token), 401)


def test_a_token_stops_working_when_it_expires(rig: Rig) -> None:
    """Renewal stops when a worker dies, so its orphaned sandbox loses the model."""
    grant = _open(rig, ttl=120)
    rig.clock.now += 121
    _assert_refused(rig, rig.ask(grant.token), 401)
    with pytest.raises(ModelProxyError, match="409"):
        rig.client.extend(grant.run_id, 120)  # a lapsed run can't be revived


def test_renewing_keeps_a_token_alive(rig: Rig) -> None:
    grant = _open(rig, ttl=120)
    rig.clock.now += 100
    rig.client.extend(grant.run_id, 120)
    rig.clock.now += 100
    assert rig.ask(grant.token)[0] == 200


def test_a_token_stops_working_when_its_run_is_closed(rig: Rig) -> None:
    grant = _open(rig)
    rig.client.close(grant.run_id)
    _assert_refused(rig, rig.ask(grant.token), 401)


def test_a_token_only_works_for_its_model(rig: Rig) -> None:
    grant = _open(rig, model=OPUS)
    _assert_refused(rig, rig.ask(grant.token, model="claude-fable-5-1"), 403)


def test_a_run_over_budget_is_refused(rig: Rig) -> None:
    grant = _open(rig, budget="0.008")
    assert rig.ask(grant.token)[0] == 200  # $0.008008: over, by at most the one request in flight
    rig.upstream.requests.clear()
    reply = rig.ask(grant.token)
    _assert_refused(rig, reply, 403)
    assert b"run budget exhausted" in reply[1]
    assert rig.client.close(grant.run_id).budget_refusals == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/models"),
        ("GET", "/v1/messages"),
        ("POST", "/v1/files"),
        ("POST", "/v1/messages/batches"),
        ("GET", "/v1/organizations/me"),
        ("POST", "/v1/messages/../organizations/me"),
        ("POST", "//evil.example/v1/messages"),
        ("DELETE", "/v1/messages"),
        ("PUT", "/v1/messages"),
    ],
)
def test_only_the_messages_endpoints_are_forwarded(rig: Rig, method: str, path: str) -> None:
    grant = _open(rig)
    assert rig.ask(grant.token, path=path, method=method)[0] == 403
    assert rig.upstream.requests == []


def test_a_run_token_cannot_use_the_admin_side(rig: Rig) -> None:
    grant = _open(rig)
    for path in ("/runs", f"/runs/{grant.run_id}/extend", f"/runs/{grant.run_id}/close"):
        for token in (grant.token, None, ""):
            request = urllib.request.Request(
                rig.url + path,
                data=b'{"budget_usd": "99", "ttl_seconds": 99999, "model": null}',
                method="POST",
                headers={"authorization": f"Bearer {token}"} if token is not None else {},
            )
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(request, timeout=5)
            assert e.value.code == 401


def test_an_oversized_body_is_refused_without_being_read(rig: Rig) -> None:
    host, port = rig.url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/v1/messages")
    conn.putheader("content-length", str(1024**3))
    conn.endheaders()
    assert conn.getresponse().status == 413


def test_a_body_that_is_not_json_is_refused(rig: Rig) -> None:
    grant = _open(rig)
    request = urllib.request.Request(
        rig.url + "/v1/messages",
        data=b"not json",
        method="POST",
        headers={"x-api-key": grant.token},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(request, timeout=5)
    assert e.value.code == 400 and rig.upstream.requests == []


# ---- streaming and metering -----------------------------------------------------------------


def test_a_stream_passes_through_byte_for_byte(rig: Rig) -> None:
    grant = _open(rig)
    status, body, headers = rig.ask(grant.token)
    assert status == 200 and body == SSE
    assert headers["content-type"] == "text/event-stream"


def test_a_stream_is_not_buffered(rig: Rig) -> None:
    """The first bytes arrive while Anthropic is still sending: the agent sees progress."""
    rig.upstream.gate = threading.Event()
    rig.upstream.chunk_size = 20
    grant = _open(rig)
    host, port = rig.url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.request("POST", "/v1/messages", json.dumps({"model": OPUS}), {"x-api-key": grant.token})
    reply = conn.getresponse()
    first = reply.read1(20)  # would time out if the proxy waited for the whole reply
    assert first == SSE[: len(first)] and first
    rig.upstream.gate.set()
    assert first + reply.read() == SSE


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 65536])
def test_usage_is_metered_whatever_the_chunk_boundaries(rig: Rig, chunk_size: int) -> None:
    rig.upstream.chunk_size = chunk_size
    grant = _open(rig)
    assert rig.ask(grant.token)[1] == SSE
    metered = rig.client.close(grant.run_id)
    assert metered.cost_usd == SSE_COST
    assert (metered.input_tokens, metered.output_tokens) == (6002, 100)
    assert metered.requests == 1


def test_a_plain_json_reply_is_metered_too(rig: Rig) -> None:
    rig.upstream.body = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 10}}).encode()
    rig.upstream.content_type = "application/json"
    grant = _open(rig)
    rig.ask(grant.token)
    assert rig.client.close(grant.run_id).cost_usd == Decimal("0.0042")


def test_counting_tokens_costs_nothing(rig: Rig) -> None:
    rig.upstream.body = json.dumps({"input_tokens": 500}).encode()
    rig.upstream.content_type = "application/json"
    grant = _open(rig)
    assert rig.ask(grant.token, path="/v1/messages/count_tokens")[0] == 200
    assert rig.client.close(grant.run_id).cost_usd == 0


def test_a_reply_cut_off_midway_is_charged_for_what_was_seen(rig: Rig) -> None:
    rig.upstream.cut_after = SSE.index(b"event: message_delta")
    grant = _open(rig)
    rig.ask(grant.token)
    metered = rig.client.close(grant.run_id)
    assert (metered.input_tokens, metered.output_tokens) == (6002, 3)
    assert metered.cost_usd > 0


def test_anthropics_errors_pass_through(rig: Rig) -> None:
    rig.upstream.status = 429
    rig.upstream.body = b'{"type":"error","error":{"type":"rate_limit_error"}}'
    rig.upstream.content_type = "application/json"
    rig.upstream.extra_headers = {"retry-after": "7"}
    grant = _open(rig)
    status, body, headers = rig.ask(grant.token)
    assert status == 429 and b"rate_limit_error" in body and headers["retry-after"] == "7"
    assert rig.client.close(grant.run_id).cost_usd == 0


def test_an_unreachable_model_api_is_reported_and_costs_nothing(
    upstream: tuple[Upstream, str],
) -> None:
    runs = Runs()
    server = serve(runs, REAL_KEY, ADMIN, "http://127.0.0.1:9", ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    grant = ModelProxyClient(url, ADMIN).open(Decimal(1), OPUS, 120)
    request = urllib.request.Request(
        url + "/v1/messages",
        data=json.dumps({"model": OPUS}).encode(),
        method="POST",
        headers={"x-api-key": grant.token},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(request, timeout=5)
    assert e.value.code == 502
    assert ModelProxyClient(url, ADMIN).close(grant.run_id).cost_usd == 0
    server.shutdown()
    server.server_close()


def test_parallel_runs_are_counted_separately(rig: Rig) -> None:
    grants = [_open(rig) for _ in range(4)]

    def work(token: str) -> None:
        for _ in range(5):
            assert rig.ask(token)[0] == 200

    threads = [threading.Thread(target=work, args=(g.token,)) for g in grants]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for g in grants:
        metered = rig.client.close(g.run_id)
        assert metered.requests == 5 and metered.cost_usd == SSE_COST * 5


def test_closing_twice_gives_the_same_answer(rig: Rig) -> None:
    grant = _open(rig)
    rig.ask(grant.token)
    assert rig.client.close(grant.run_id) == rig.client.close(grant.run_id)


def test_the_sse_meter_ignores_damaged_lines() -> None:
    meter = SseMeter()
    meter.feed(b'data: {"usage": broken\n\ndata: {"usage": {"output_tokens": 5}}\n\ndata: [1]')
    assert meter.usage == {"output_tokens": 5}


# ---- the rules on their own -----------------------------------------------------------------


@pytest.mark.parametrize("budget", ["0", "-1"])
def test_a_run_needs_a_budget(budget: str) -> None:
    with pytest.raises(Refused, match="budget"):
        Runs().open(Decimal(budget), OPUS, 120)


def test_unknown_models_are_charged_as_the_dearest() -> None:
    runs = Runs()
    run = runs.open(Decimal(1), None, 120)
    runs.record(run, "claude-new", {"input_tokens": 1_000_000})
    assert run.cost_usd == Decimal(10)


def test_old_runs_are_forgotten_after_a_day() -> None:
    clock = Clock()
    runs = Runs(clock=clock)
    old = runs.open(Decimal(1), None, 60)
    clock.now += RETENTION_SECONDS + 61
    runs.open(Decimal(1), None, 60)
    with pytest.raises(Refused, match="unknown"):
        runs.close(old.id)


# ---- the sandbox runtime --------------------------------------------------------------------


def _request(tmp_path: Path) -> RunRequest:
    (tmp_path / "repo").mkdir()
    return RunRequest(
        task_key="AX-1",
        title="t",
        specification="s",
        acceptance_criteria=[],
        allowed_paths=[],
        workspace=tmp_path / "repo",
        output_dir=tmp_path / "out",
        budget_usd=Decimal(1),
        model=OPUS,
    )


FORGED = {
    "outcome": "failed",
    "error": "x",
    "cost_usd": "0.00",  # the sandbox claims it spent nothing
    "input_tokens": 1,
    "output_tokens": 1,
}


def _fake_container(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, *, spend: int = 1, done: Any = None
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def run_container(self: SandboxRuntime, **kwargs: Any) -> Any:
        seen.update(kwargs)
        env = dict(item.split("=", 1) for item in kwargs["extra_env"])
        seen["env"] = env
        seen["file"] = kwargs["output_dir"].parent / MODEL_RUN_FILE
        for _ in range(spend):
            assert rig.ask(env["ANTHROPIC_API_KEY"])[0] in (200, 403)
        if isinstance(done, Exception):
            raise done
        return done or FORGED

    monkeypatch.setattr(SandboxRuntime, "_run_container", run_container)
    return seen


def test_a_sandbox_gets_a_run_token_and_never_the_key(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", REAL_KEY)
    seen = _fake_container(rig, monkeypatch)
    SandboxRuntime(model_proxy=rig.client).run(_request(tmp_path))
    assert seen["env"]["ANTHROPIC_API_KEY"].startswith("run_")
    assert seen["env"]["ANTHROPIC_BASE_URL"] == rig.url
    assert seen["env"]["NO_PROXY"] == "127.0.0.1"
    assert REAL_KEY not in json.dumps(seen["extra_env"])


def test_the_docker_command_carries_no_key_from_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", REAL_KEY)
    args = SandboxRuntime().docker_args(_request(tmp_path), "c")
    assert "ANTHROPIC_API_KEY" not in args and REAL_KEY not in " ".join(args)


def test_a_forged_cost_is_ignored(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox writes outcome.json, and said $0. The record is what the proxy counted."""
    _fake_container(rig, monkeypatch, spend=2)
    outcome = SandboxRuntime(model_proxy=rig.client).run(_request(tmp_path))
    assert outcome.cost_usd == SSE_COST * 2 and outcome.cost_estimated is False
    assert outcome.output_tokens == 200


def test_a_review_run_is_metered_the_same_way(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repo").mkdir()
    _fake_container(rig, monkeypatch, done={"outcome": "failed", "cost_usd": "0"})
    request = ReviewRequest(
        task_key="AX-1",
        review_id=1,
        role="code_reviewer",
        title="t",
        specification="s",
        acceptance_criteria=[],
        allowed_paths=[],
        diff="",
        developer_result=None,
        workspace=tmp_path / "repo",
        output_dir=tmp_path / "out",
        budget_usd=Decimal(1),
        model=OPUS,
    )
    assert SandboxRuntime(model_proxy=rig.client).run_review(request).cost_usd == SSE_COST


def test_a_run_refused_by_its_budget_says_so(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_container(rig, monkeypatch, spend=3)
    request = _request(tmp_path)
    request = RunRequest(**{**request.__dict__, "budget_usd": Decimal("0.008")})
    outcome = SandboxRuntime(model_proxy=rig.client).run(request)
    assert outcome.outcome == Outcome.BUDGET_EXCEEDED and outcome.error == "budget exceeded"


def test_a_run_is_closed_even_if_the_container_fails(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_container(rig, monkeypatch, done=RuntimeError("docker broke"))
    with pytest.raises(RuntimeError):
        SandboxRuntime(model_proxy=rig.client).run(_request(tmp_path))
    assert rig.ask(seen["env"]["ANTHROPIC_API_KEY"])[0] == 401


def test_a_running_sandbox_keeps_its_run_alive(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extended = threading.Event()
    real_extend = ModelProxyClient.extend

    def extend(self: ModelProxyClient, run_id: str, ttl: float) -> None:
        real_extend(self, run_id, ttl)
        extended.set()

    monkeypatch.setattr(ModelProxyClient, "extend", extend)

    def run_container(self: SandboxRuntime, **kwargs: Any) -> Any:
        assert extended.wait(5)
        return FORGED

    monkeypatch.setattr(SandboxRuntime, "_run_container", run_container)
    SandboxRuntime(model_proxy=rig.client, model_ttl_seconds=0.3).run(_request(tmp_path))
    assert extended.is_set()


def test_no_proxy_means_no_model_access_rather_than_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = ModelProxyClient("http://127.0.0.1:9", ADMIN)
    monkeypatch.setattr(SandboxRuntime, "_run_container", lambda self, **k: pytest.fail("started"))
    with pytest.raises(ModelProxyError):
        SandboxRuntime(model_proxy=down).run(_request(tmp_path))


def test_the_run_file_is_where_the_sandbox_cannot_change_it(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_container(rig, monkeypatch)
    request = _request(tmp_path)
    SandboxRuntime(model_proxy=rig.client).run(request)
    assert seen["file"].parent == tmp_path  # beside out/ and repo/, the only mounts
    assert request.output_dir not in seen["file"].parents
    assert request.workspace not in seen["file"].parents


# ---- a worker killed mid-run ----------------------------------------------------------------


def _killed_run(rig: Rig, tmp_path: Path) -> tuple[Path, Any]:
    run_dir = tmp_path / "AX-1-e7"
    (run_dir / "out").mkdir(parents=True)
    grant = _open(rig)
    (run_dir / MODEL_RUN_FILE).write_text(json.dumps({"run_id": grant.run_id}))
    rig.ask(grant.token)
    return run_dir, grant


def test_a_killed_runs_cost_comes_from_the_proxy_not_its_transcript(
    rig: Rig, tmp_path: Path
) -> None:
    run_dir, _ = _killed_run(rig, tmp_path)
    forged = {"type": "ResultMessage", "data": {"total_cost_usd": 0.0}}
    (run_dir / "out" / "transcript.jsonl").write_text(json.dumps(forged) + "\n")
    cost = recovered_cost(run_dir, rig.client)
    assert cost is not None
    assert cost["usd"] == str(SSE_COST) and cost["estimated"] is False


def test_without_the_proxys_record_a_transcript_is_marked_as_an_estimate(
    rig: Rig, tmp_path: Path
) -> None:
    run_dir, _ = _killed_run(rig, tmp_path)
    (run_dir / MODEL_RUN_FILE).write_text(json.dumps({"run_id": "mr_forgotten"}))
    line = {
        "type": "AssistantMessage",
        "data": {"message_id": "m", "model": OPUS, "usage": {"input_tokens": 1000}},
    }
    (run_dir / "out" / "transcript.jsonl").write_text(json.dumps(line) + "\n")
    cost = recovered_cost(run_dir, rig.client)
    assert cost is not None and cost["estimated"] is True


def test_a_refused_run_reports_the_real_error_not_success() -> None:
    """Found in the H0 spike: an API error ends a turn normally, and read "success: success: "."""
    result = SimpleNamespace(
        usage={},
        total_cost_usd=0.0,
        num_turns=1,
        subtype="success",
        is_error=True,
        errors=[],
        result="API Error: 403 run budget exhausted",
        structured_output=None,
    )
    outcome = to_outcome(result, Path("t"), "success: ")
    assert outcome.outcome == Outcome.FAILED
    assert outcome.error == "API error: API Error: 403 run budget exhausted"
