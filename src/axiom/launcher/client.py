"""The worker's side of the launcher."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from axiom.launcher.runner import LaunchResult
from axiom.launcher.spec import ContainerSpec

GRACE_SECONDS = 180  # the launcher enforces the timeout; this only covers its kill and reply


class LauncherError(Exception):
    pass


@dataclass(frozen=True)
class HttpLauncher:
    url: str
    token: str

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                data: dict[str, Any] = json.loads(reply.read())
                return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LauncherError(f"launcher refused ({e.code}): {detail}") from None
        except (TimeoutError, OSError, ValueError) as e:
            raise LauncherError(f"launcher unreachable: {type(e).__name__}") from None

    def run(self, spec: ContainerSpec, timeout_seconds: float) -> LaunchResult:
        data = self._post(
            "/containers/run",
            spec.to_json() | {"timeout_seconds": timeout_seconds},
            timeout_seconds + GRACE_SECONDS,
        )
        return LaunchResult(
            int(data["exit_code"]), str(data.get("output", "")), bool(data.get("timed_out"))
        )

    def kill(self, name: str) -> bool:
        return bool(self._post(f"/containers/{name}/kill", {}, 90).get("killed"))
