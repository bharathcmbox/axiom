"""The worker's side of the model proxy. Holds the admin token; never sees the Anthropic key."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit


class ModelProxyError(Exception):
    pass


@dataclass(frozen=True)
class Grant:
    run_id: str
    token: str


@dataclass(frozen=True)
class Metered:
    requests: int
    budget_refusals: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int

    def cost_payload(self) -> dict[str, Any]:
        return {
            "usd": str(self.cost_usd),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated": False,
        }


@dataclass(frozen=True)
class ModelProxyClient:
    url: str  # how this worker reaches the proxy
    admin_token: str
    sandbox_url: str | None = None  # how a sandbox reaches it, when that differs (live tests)

    @property
    def sandbox_address(self) -> str:
        return self.sandbox_url or self.url

    @property
    def sandbox_host(self) -> str:
        return urlsplit(self.sandbox_address).hostname or ""

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.admin_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                data: dict[str, Any] = json.loads(reply.read())
                return data
        except urllib.error.HTTPError as e:
            raise ModelProxyError(f"model proxy {path}: HTTP {e.code}") from None
        except (OSError, ValueError) as e:
            raise ModelProxyError(f"model proxy {path}: {type(e).__name__}") from None

    def open(self, budget_usd: Decimal, model: str | None, ttl_seconds: float) -> Grant:
        data = self._post(
            "/runs", {"budget_usd": str(budget_usd), "model": model, "ttl_seconds": ttl_seconds}
        )
        return Grant(data["run_id"], data["token"])

    def extend(self, run_id: str, ttl_seconds: float) -> None:
        self._post(f"/runs/{run_id}/extend", {"ttl_seconds": ttl_seconds})

    def close(self, run_id: str) -> Metered:
        d = self._post(f"/runs/{run_id}/close", {})
        return Metered(
            int(d["requests"]),
            int(d["budget_refusals"]),
            Decimal(d["cost_usd"]),
            int(d["input_tokens"]),
            int(d["output_tokens"]),
        )
