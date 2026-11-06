#!/usr/bin/env python3
"""Refuse a commit that contains a real secret. Standard library only; can run as a git hook.

Compares the staged change with the actual values in the local secret files, so dummy values in
tests don't trip it. Reports only the name of what matched, never the value.
"""

import re
import subprocess
import sys
from pathlib import Path

SECRET_FILES = (Path(".env"), Path.home() / ".axiom" / "human.env")
MIN_LENGTH = 12  # shorter values would match by chance


def secrets_in(files: tuple[Path, ...]) -> dict[str, str]:
    found = {}
    for path in files:
        if path.is_file():
            for name, value in re.findall(r"^([A-Za-z_0-9]+)=(\S+)", path.read_text(), re.M):
                if len(value) >= MIN_LENGTH:
                    found[name] = value
    return found


def leaks(text: str, secrets: dict[str, str]) -> list[str]:
    return sorted(name for name, value in secrets.items() if value in text)


def main() -> int:
    staged = subprocess.run(
        ["git", "diff", "--cached", "--no-color"], capture_output=True, text=True, check=True
    ).stdout
    paths = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True
    ).stdout.split()
    problems = [
        f"{name} is in the staged change" for name in leaks(staged, secrets_in(SECRET_FILES))
    ]
    problems += [f"{path} must not be committed" for path in paths if Path(path).name == ".env"]
    for problem in problems:
        print(f"check_secrets: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
