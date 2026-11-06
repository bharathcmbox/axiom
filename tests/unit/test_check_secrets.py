"""The secret check: real values refuse a commit; dummy values in tests don't."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_secrets.py"
spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
assert spec is not None and spec.loader is not None
check_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_secrets)
REAL = "real-value-0123456789abcdef"


def _repo(tmp_path: Path, env: str = f"TOKEN={REAL}\n") -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text(env)
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True, check=False
    )


def _stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    subprocess.run(["git", "add", "-f", name], cwd=repo, check=True)


def test_a_real_value_refuses_the_commit_and_is_never_printed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _stage(repo, "a.py", f"key = '{REAL}'\n")
    done = _run(repo)
    assert done.returncode == 1
    assert "TOKEN is in the staged change" in done.stderr
    assert REAL not in done.stderr + done.stdout


def test_a_dummy_value_in_a_test_passes(tmp_path: Path) -> None:
    """The false alarm that prompted this: github_pat_SHOULD_NOT_LEAK in an existing test."""
    repo = _repo(tmp_path)
    _stage(repo, "test.py", "x = 'github_pat_SHOULD_NOT_LEAK'\n")
    assert _run(repo).returncode == 0


def test_committing_the_env_file_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "TOKEN=short\n")
    subprocess.run(["git", "add", "-f", ".env"], cwd=repo, check=True)
    done = _run(repo)
    assert done.returncode == 1 and ".env must not be committed" in done.stderr


def test_short_values_are_ignored_as_too_likely_to_match_by_chance(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PORT=8000\nTOKEN=abcdefghijklmnop\n")
    assert check_secrets.secrets_in((tmp_path / ".env",)) == {"TOKEN": "abcdefghijklmnop"}


def test_leaks_names_only_what_appears() -> None:
    secrets = {"A": "value-a-0123456789", "B": "value-b-0123456789"}
    assert check_secrets.leaks("x value-b-0123456789 y", secrets) == ["B"]
