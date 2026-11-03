"""Phase 2 T4: the result contract accepts good reports and rejects malformed ones."""

from typing import Any

import pytest

from axiom.contracts import InvalidResult, parse_result, result_json_schema

GOOD: dict[str, Any] = {
    "status": "submitted",
    "summary": "Added pytest as a dev dependency",
    "files_changed": ["agent/pyproject.toml", "agent/uv.lock"],
    "tests_run": {"command": "uv run pytest -q", "passed": 8, "failed": 0},
    "criteria": [{"criterion": "uv run pytest works", "met": True, "evidence": "8 passed"}],
    "known_limitations": [],
}


def test_valid_result_parses() -> None:
    r = parse_result(GOOD)
    assert r.status == "submitted" and r.tests_run is not None and r.tests_run.passed == 8


def test_needs_input_requires_a_question() -> None:
    assert parse_result({"status": "needs_input", "summary": "s", "question": "Which DB?"})
    with pytest.raises(InvalidResult, match="question"):
        parse_result({"status": "needs_input", "summary": "s"})
    with pytest.raises(InvalidResult, match="question"):
        parse_result(GOOD | {"question": "why?"})


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "done"},
        {"summary": ""},
        {"tests_run": {"command": "x", "passed": -1, "failed": 0}},
        {"criteria": [{"criterion": "c", "met": "yes", "evidence": "e"}]},
        {"pr_url": "https://evil.example"},  # unknown fields are rejected, not ignored
        {"files_changed": "a.py"},
    ],
)
def test_malformed_results_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidResult):
        parse_result(GOOD | bad)


def test_non_object_rejected() -> None:
    with pytest.raises(InvalidResult):
        parse_result(["not", "an", "object"])


def test_json_schema_is_strict() -> None:
    schema = result_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "summary"}


@pytest.mark.parametrize(
    "bad",
    [
        {"criteria": [{"criterion": "c", "met": 1, "evidence": "e"}]},
        {"tests_run": {"command": "x", "passed": "8", "failed": 0}},
    ],
)
def test_no_silent_type_coercion(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidResult):
        parse_result(GOOD | bad)
