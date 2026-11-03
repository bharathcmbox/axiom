"""Run inside the sandbox: report which network destinations are reachable."""

import socket
import urllib.error
import urllib.request


def probe(url: str, use_proxy: bool = True) -> str:
    handlers = [] if use_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    try:
        r = opener.open(url, timeout=10)
        return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} (reached server)"
    except Exception as e:
        return f"BLOCKED {type(e).__name__} {str(e)[:80]}"


def raw_tcp(host: str, port: int) -> str:
    try:
        socket.create_connection((host, port), timeout=5)
        return "OPEN"
    except Exception as e:
        return f"BLOCKED {type(e).__name__}"


print("anthropic via proxy =", probe("https://api.anthropic.com/v1/models"))
print("pypi via proxy =", probe("https://pypi.org/simple/pytest/"))
print("github via proxy =", probe("https://github.com/"))
print("example via proxy =", probe("https://example.com/"))
print("anthropic no proxy =", probe("https://api.anthropic.com/v1/models", use_proxy=False))
print("raw tcp 1.1.1.1 port 443 =", raw_tcp("1.1.1.1", 443))
