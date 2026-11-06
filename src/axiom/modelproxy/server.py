"""The proxy's HTTP side. Standard library only, and deliberately small: it holds the key.

Never logs headers or bodies. Forwards only the Messages endpoints, only to the one upstream.
Replies pass through as they arrive, so streaming behaves as if the proxy weren't there. Metering
reads a copy of the stream and can never change it.
"""

import hmac
import http.client
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from axiom.modelproxy.runs import Refused, Runs

log = logging.getLogger("axiom.modelproxy")
FORWARDED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
MAX_BODY_BYTES = 64 * 1024 * 1024
DROPPED_REQUEST_HEADERS = {
    "host", "x-api-key", "authorization", "connection", "content-length", "proxy-connection",
    "accept-encoding", "keep-alive", "te", "transfer-encoding", "upgrade",
}  # fmt: skip
DROPPED_REPLY_HEADERS = {"transfer-encoding", "connection", "content-length", "keep-alive"}


class SseMeter:
    """Usage from a stream, whatever the chunk boundaries: message_start, then message_delta."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self._rest = b""

    def feed(self, chunk: bytes) -> None:
        self._rest += chunk
        lines = self._rest.split(b"\n")
        self._rest = lines.pop()
        for line in lines:
            if line.startswith(b"data:") and b"usage" in line:
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                message = data.get("message")
                usage = message.get("usage") if isinstance(message, dict) else data.get("usage")
                if isinstance(usage, dict):
                    self.usage.update(usage)


def _usage_of_json_reply(body: bytes) -> dict[str, Any]:
    try:
        usage = json.loads(body).get("usage")
    except ValueError, AttributeError:
        return {}
    return usage if isinstance(usage, dict) else {}


def make_handler(
    runs: Runs, api_key: str, admin_token: str, upstream: str
) -> type[BaseHTTPRequestHandler]:
    target = urlsplit(upstream)
    connection = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "axiom-model-proxy"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass  # the default logs request lines; our own log says only what we choose

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, status: int, message: str) -> None:
            """In Anthropic's error shape, so the agent's CLI reports it instead of crashing."""
            kind = "authentication_error" if status == 401 else "permission_error"
            self._json(status, {"type": "error", "error": {"type": kind, "message": message}})

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                self._refuse(413, "request is too large")
                return None
            return self.rfile.read(length) if length else b""

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(200, {"status": "ok"})
            else:
                self._refuse(403, "only the Messages endpoints are available")

        def do_POST(self) -> None:
            body = self._read_body()
            if body is None:
                return
            path = urlsplit(self.path).path
            try:
                if path == "/runs" or path.startswith("/runs/"):
                    self._admin(path, body)
                elif path in FORWARDED_PATHS:
                    self._forward(path, body)
                else:
                    self._refuse(403, "only the Messages endpoints are available")
            except Refused as e:
                self._refuse(e.status, e.message)

        def _refuse_method(self) -> None:
            self.close_connection = True
            self._refuse(403, "only the Messages endpoints are available")

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse_method

        # ---- the worker's side --------------------------------------------------------------

        def _admin(self, path: str, body: bytes) -> None:
            given = (self.headers.get("authorization") or "").removeprefix("Bearer ")
            if not hmac.compare_digest(given.encode(), admin_token.encode()):
                raise Refused(401, "admin token is not valid")
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
                parts = path.strip("/").split("/")
                if parts == ["runs"]:
                    run = runs.open(
                        Decimal(str(data["budget_usd"])),
                        data.get("model"),
                        float(data["ttl_seconds"]),
                    )
                    self._json(200, {"run_id": run.id, "token": run.token})
                elif len(parts) == 3 and parts[2] == "extend":
                    runs.extend(parts[1], float(data["ttl_seconds"]))
                    self._json(200, {"run_id": parts[1]})
                elif len(parts) == 3 and parts[2] == "close":
                    self._json(200, runs.close(parts[1]))
                else:
                    raise Refused(404, "unknown admin path")
            except (KeyError, ValueError, InvalidOperation) as e:
                raise Refused(422, f"invalid request: {type(e).__name__}") from None

        # ---- the agent's side ---------------------------------------------------------------

        def _forward(self, path: str, body: bytes) -> None:
            bearer = (self.headers.get("authorization") or "").removeprefix("Bearer ")
            token = self.headers.get("x-api-key") or bearer
            try:
                request = json.loads(body or b"{}")
                model = request.get("model") if isinstance(request, dict) else None
            except ValueError:
                raise Refused(400, "request body is not JSON") from None
            counting = path.endswith("/count_tokens")
            run = runs.admit(token, model)
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in DROPPED_REQUEST_HEADERS
            }
            headers |= {
                "x-api-key": api_key,
                "content-length": str(len(body)),
                "accept-encoding": "identity",  # the meter has to be able to read the reply
            }
            conn = connection(target.netloc, timeout=900)
            meter, whole = SseMeter(), bytearray()
            try:
                try:
                    conn.request("POST", self.path, body, headers)
                    reply = conn.getresponse()
                except OSError as e:
                    log.warning("upstream unreachable: %s", type(e).__name__)
                    raise Refused(502, "the model API could not be reached") from None
                self.send_response(reply.status)
                for name, value in reply.getheaders():
                    if name.lower() not in DROPPED_REPLY_HEADERS:
                        self.send_header(name, value)
                self.send_header("transfer-encoding", "chunked")
                self.send_header("connection", "close")
                self.end_headers()
                self.close_connection = True
                is_stream = "text/event-stream" in (reply.getheader("content-type") or "")
                try:
                    while chunk := reply.read1(65536):
                        if is_stream:
                            meter.feed(chunk)
                        elif len(whole) < MAX_BODY_BYTES:
                            whole += chunk
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError as e:  # either side went away: charge for what was seen
                    log.warning("stream ended early: %s", type(e).__name__)
                usage = meter.usage if is_stream else _usage_of_json_reply(bytes(whole))
                if usage and not counting:
                    runs.record(run, model if isinstance(model, str) else None, usage)
                log.info("run %s: %s -> %s", run.id, path, reply.status)
            finally:
                conn.close()

    return Handler


def serve(
    runs: Runs, api_key: str, admin_token: str, upstream: str, address: tuple[str, int]
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(address, make_handler(runs, api_key, admin_token, upstream))
    server.daemon_threads = True
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Taken out of the environment at once: nothing started from here can inherit them.
    api_key = os.environ.pop("ANTHROPIC_API_KEY", "")
    admin_token = os.environ.pop("AXIOM_MODEL_PROXY_ADMIN_TOKEN", "")
    if not api_key or not admin_token:
        raise SystemExit("model proxy misconfigured: needs its API key and admin token")
    upstream = os.environ.get("AXIOM_MODEL_PROXY_UPSTREAM", "https://api.anthropic.com")
    server = serve(Runs(), api_key, admin_token, upstream, ("0.0.0.0", 8080))
    log.info("model proxy listening")
    server.serve_forever()
    return 0
