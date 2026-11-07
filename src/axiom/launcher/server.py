"""The launcher's HTTP side. Reachable only by workers, with a token only workers hold."""

import hmac
import json
import logging
import os
import select
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from axiom.launcher.runner import LocalLauncher
from axiom.launcher.spec import MAX_TIMEOUT_SECONDS, ContainerSpec, InvalidSpec, Policy

log = logging.getLogger("axiom.launcher")
MAX_BODY_BYTES = 1024 * 1024


def make_handler(launcher: LocalLauncher, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "axiom-launcher"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _caller_gone(self) -> bool:
            """The worker holds this request open for as long as the sandbox runs."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and self.connection.recv(1, 2) == b""  # 2: MSG_PEEK
            except OSError:
                return True

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(200, {"status": "ok"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                return self._json(413, {"error": "request is too large"})
            body = self.rfile.read(length) if length else b""
            given = (self.headers.get("authorization") or "").removeprefix("Bearer ")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return self._json(401, {"error": "token is not valid"})
            parts = self.path.strip("/").split("/")
            try:
                data = json.loads(body or b"{}")
                if parts == ["containers", "run"]:
                    self._run(data)
                elif len(parts) == 3 and parts[0] == "containers" and parts[2] == "kill":
                    self._json(200, {"killed": launcher.kill(parts[1])})
                else:
                    self._json(404, {"error": "not found"})
            except (ValueError, InvalidSpec) as e:
                message = str(e) if isinstance(e, InvalidSpec) else "request is not valid JSON"
                log.warning("refused: %s", message)
                self._json(422, {"error": message})

        def _run(self, data: Any) -> None:
            if not isinstance(data, dict):
                raise InvalidSpec("the request must be an object")
            fields = dict(data)
            try:
                timeout = float(fields.pop("timeout_seconds"))
            except KeyError, TypeError, ValueError:
                raise InvalidSpec("timeout_seconds is required") from None
            if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
                raise InvalidSpec("timeout_seconds is out of range")
            spec = ContainerSpec.from_json(fields)
            log.info("starting %s", spec.name)
            result = launcher.run(spec, timeout, caller_gone=self._caller_gone)
            log.info(
                "%s exited %s%s",
                spec.name,
                result.exit_code,
                " (caller gone, killed)" if result.abandoned else "",
            )
            self._json(
                200,
                {
                    "exit_code": result.exit_code,
                    "output": result.output,
                    "timed_out": result.timed_out,
                },
            )

    return Handler


def serve(launcher: LocalLauncher, token: str, address: tuple[str, int]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(address, make_handler(launcher, token))
    server.daemon_threads = True
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.pop("AXIOM_LAUNCHER_TOKEN", "")
    root = os.environ.get("AXIOM_WORKSPACES_DIR", "")
    if not token or not root:
        raise SystemExit("launcher misconfigured: needs its token and AXIOM_WORKSPACES_DIR")
    policy = Policy(
        image=os.environ.get("AXIOM_SANDBOX_IMAGE", "axiom-sandbox:latest"),
        network=os.environ.get("AXIOM_SANDBOX_NETWORK", "axiom_sandbox"),
        workspaces_root=Path(root),
    )
    server = serve(LocalLauncher(policy), token, ("0.0.0.0", 8090))
    log.info("launcher listening")
    server.serve_forever()
    return 0
