"""Docs stay honest: every test the guide cites by name exists."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_test_named_in_the_guide_exists() -> None:
    defined = set()
    for path in (ROOT / "tests").rglob("*.py"):
        defined |= set(re.findall(r"def (test_\w+)", path.read_text()))
    defined |= {"test_command", "test_database_url"}  # settings fields, not tests
    missing = {}
    for doc in (ROOT / "docs" / "guide").glob("*.md"):
        named = set(re.findall(r"`(test_[a-z0-9_]+)`", doc.read_text()))
        if named - defined:
            missing[doc.name] = sorted(named - defined)
    assert not missing
