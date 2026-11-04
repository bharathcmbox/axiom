"""The worker's view of the Axiom API: an ordinary authenticated HTTP client (agent token)."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.code = body.get("error", "")
        super().__init__(f"{status} {self.code}: {body.get('detail', body)}")


@dataclass
class AxiomClient:
    base_url: str
    token: str
    timeout: int = 30

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                payload = {}
            raise ApiError(e.code, payload if isinstance(payload, dict) else {}) from None

    def claim(self, lease_seconds: int, project_key: str | None = None) -> dict[str, Any] | None:
        return self._call(  # type: ignore[no-any-return]
            "POST", "/claims", {"lease_seconds": lease_seconds, "project_key": project_key}
        )

    def command(
        self, key: str, command: str, payload: dict[str, Any] | None = None, **extra: Any
    ) -> dict[str, Any]:
        body = {"payload": payload or {}} | extra
        return self._call("POST", f"/tasks/{key}/commands/{command}", body)  # type: ignore[no-any-return]

    def heartbeat(self, key: str, lease_seconds: int) -> None:
        self._call("POST", f"/tasks/{key}/heartbeat", {"lease_seconds": lease_seconds})

    def task(self, key: str) -> dict[str, Any]:
        return self._call("GET", f"/tasks/{key}")  # type: ignore[no-any-return]

    def executions(self, key: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/tasks/{key}/executions")  # type: ignore[no-any-return]

    def events(self, key: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/tasks/{key}/events")  # type: ignore[no-any-return]

    def project_by_id(self, project_id: int) -> dict[str, Any]:
        for p in self._call("GET", "/projects"):
            if p["id"] == project_id:
                return p  # type: ignore[no-any-return]
        raise ApiError(404, {"error": "not_found", "detail": f"project {project_id}"})

    def agent(self, key: str) -> dict[str, Any]:
        return self._call("GET", f"/agents/{key}")  # type: ignore[no-any-return]
