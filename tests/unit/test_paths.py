"""allowed_paths matching and secret-file detection, shared by sandbox hooks and the harness."""

import pytest

from axiom.paths import is_allowed, is_secret, normalize

ALLOWED = ["agent/pyproject.toml", "CLAUDE.md", "agent/tests/", "docs/**/*.md"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("agent/pyproject.toml", True),
        ("./agent/pyproject.toml", True),
        ("CLAUDE.md", True),
        ("agent/tests/test_x.py", True),
        ("agent/tests/deep/test_y.py", True),
        ("docs/a.md", True),
        ("docs/phase/b.md", True),
        ("docs/a.txt", False),
        ("agent/socagent/llm.py", False),
        ("agent/pyproject.toml.bak", False),
        ("README.md", False),
        ("agent/tests/../socagent/llm.py", False),  # normalizes outside the allowed dir
        ("../CLAUDE.md", False),
        ("/etc/passwd", False),
        ("agent/tests/.env", False),  # secret, even inside an allowed directory
    ],
)
def test_is_allowed(path: str, expected: bool) -> None:
    assert is_allowed(path, ALLOWED) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", True),
        ("agent/.env", True),
        (".env.local", True),
        (".env.example", False),
        ("lab/.secrets/console-password", True),
        ("lab/.secrets", True),
        ("certs/server.pem", True),
        ("keys/id_ed25519.pub", True),
        (".git/config", True),
        ("agent/socagent/config.py", False),
        ("../outside", True),
    ],
)
def test_is_secret(path: str, expected: bool) -> None:
    assert is_secret(path) is expected


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("a/./b", "a/b"),
        ("a/b/../c", "a/c"),
        ("a\\b", "a/b"),
        ("..", None),
        ("/x", None),
        ("", None),
    ],
)
def test_normalize(raw: str, norm: str | None) -> None:
    assert normalize(raw) == norm
