"""Live tests reach the model the way production does: through a real model proxy holding the key."""

import os
import secrets
import subprocess
import time
import urllib.request
from collections.abc import Iterator

import pytest

from axiom.modelproxy.client import ModelProxyClient

NAME = "model-proxy-live"


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def model_proxy() -> Iterator[ModelProxyClient]:
    admin = secrets.token_hex(16)
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
    _docker("run", "-d", "--name", NAME, "--network", "axiom_default", "-p", "127.0.0.1::8080",
            "-e", "ANTHROPIC_API_KEY", "-e", f"AXIOM_MODEL_PROXY_ADMIN_TOKEN={admin}",
            "axiom-api:latest", "python", "-m", "axiom.modelproxy")  # fmt: skip
    try:
        _docker("network", "connect", "axiom_sandbox", NAME)
        port = _docker("port", NAME, "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{url}/healthz", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        # This process has the key only to hand it to the proxy: nothing else may see it.
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        yield ModelProxyClient(url, admin, sandbox_url=f"http://{NAME}:8080")
        if saved:
            os.environ["ANTHROPIC_API_KEY"] = saved
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
