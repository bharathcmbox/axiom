#!/usr/bin/env python3
"""What a compromised worker can actually reach, checked from inside a real worker container.

Run with `make check-boundaries`. Calls no model and changes nothing outside a scratch directory.
Prints only PASS or FAIL lines and exits non-zero on any FAIL. Never prints a credential.
"""

import os
import shutil
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from axiom.launcher.client import HttpLauncher, LauncherError
from axiom.launcher.spec import ContainerSpec

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")


def http_status(url: str, *, direct: bool = False) -> str:
    handlers = [urllib.request.ProxyHandler({})] if direct else []
    try:
        reply = urllib.request.build_opener(*handlers).open(url, timeout=10)
        return str(reply.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except OSError as e:
        return f"blocked: {type(getattr(e, 'reason', e)).__name__}"


def tcp_open(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=5).close()
        return True
    except OSError:
        return False


def docker_power() -> None:
    check("no Docker socket", not Path("/var/run/docker.sock").exists())
    check("no Docker program", shutil.which("docker") is None)


def anthropic_key() -> None:
    check("no Anthropic key in the environment", "ANTHROPIC_API_KEY" not in os.environ)


def launcher() -> None:
    client = HttpLauncher(
        os.environ.get("AXIOM_LAUNCHER_URL") or "http://sandbox-launcher:8090",
        os.environ["AXIOM_LAUNCHER_TOKEN"],
    )
    run = Path(os.environ["AXIOM_WORKSPACES_DIR"]) / f"boundary-check-{os.getpid()}"
    (run / "repo").mkdir(parents=True)
    (run / "out").mkdir()
    repo, out = str(run / "repo"), str(run / "out")
    try:
        result = client.run(
            ContainerSpec("axiom-verify-boundary", repo, out, (), ("sh", "-c", "echo marker")), 120
        )
        check("launcher starts a sandbox", result.exit_code == 0 and "marker" in result.output)
        seen = client.run(
            ContainerSpec(
                "axiom-verify-boundary2",
                repo,
                out,
                (),
                ("sh", "-c", "ls /var/run/docker.sock; ls /workspace; env"),
            ),
            120,
        ).output
        check("sandbox has no Docker socket", "No such file" in seen)
        check(
            "sandbox has no credentials",
            not any(n in seen for n in ("TOKEN", "ANTHROPIC_API_KEY", "GITHUB")),
        )
        for name, spec in {
            "host root": ContainerSpec("axiom-verify-x", "/", out),
            "another path": ContainerSpec("axiom-verify-x", "/etc/repo", "/etc/out"),
            "another container's name": ContainerSpec("axiom-db-1", repo, out),
            "a credential": ContainerSpec("axiom-verify-x", repo, out, ("AXIOM_AGENT_TOKEN=x",)),
            "another program": ContainerSpec("axiom-verify-x", repo, out, (), ("docker", "ps")),
        }.items():
            try:
                client.run(spec, 30)
                check(f"launcher refuses {name}", False)
            except LauncherError as e:
                check(f"launcher refuses {name}", "422" in str(e))
        check("launcher won't kill another container", client.kill("axiom-db-1") is False)
        try:
            HttpLauncher(client.url, "wrong").kill("x")
            check("launcher needs its token", False)
        except LauncherError as e:
            check("launcher needs its token", "401" in str(e))
    finally:
        shutil.rmtree(run, ignore_errors=True)


def egress() -> None:
    check("GitHub reachable through the proxy", http_status("https://api.github.com/") == "200")
    check("other hosts refused by the proxy", http_status("https://example.com/") == "403")
    check("Anthropic refused by the proxy", http_status("https://api.anthropic.com/") == "403")
    check(
        "no direct internet", http_status("https://example.com/", direct=True).startswith("blocked")
    )
    check("no raw internet", not tcp_open("1.1.1.1", 443))
    check("the database is out of reach", not tcp_open("db", 5432))
    check(
        "the Axiom API is reachable", http_status("http://api:8000/healthz", direct=True) == "200"
    )
    status = http_status("http://model-proxy:8080/healthz", direct=True)
    check("the model proxy is reachable", status == "200")


CHECKS = {
    "docker": docker_power,
    "key": anthropic_key,
    "launcher": launcher,
    "egress": egress,
}

if __name__ == "__main__":
    for section in sys.argv[1:] or list(CHECKS):
        CHECKS[section]()
    print(f"\n{failures} failed" if failures else "\nall boundaries hold")
    sys.exit(1 if failures else 0)
