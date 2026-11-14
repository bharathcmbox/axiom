"""Phase 5 T2: what each reviewer role must examine, as contract, schema and prompt."""

from typing import Any

import pytest

from axiom.contracts import REVIEW_CHECKLISTS, InvalidResult, parse_review, review_json_schema
from axiom.sandbox.review_prompt import checklist_text

ROLES = sorted(REVIEW_CHECKLISTS)


def _review(role: str, result: str = "ok", **extra: Any) -> dict[str, Any]:
    checklist = {i: {"result": result, "note": "looked"} for i in REVIEW_CHECKLISTS[role]}
    return {"verdict": "PASS", "summary": "fine", "checklist": checklist, "findings": []} | extra


@pytest.mark.parametrize("role", ROLES)
def test_a_complete_checklist_is_accepted(role: str) -> None:
    assert set(parse_review(_review(role), role).checklist) == set(REVIEW_CHECKLISTS[role])


@pytest.mark.parametrize("role", ROLES)
def test_one_roles_checklist_doesnt_satisfy_the_other(role: str) -> None:
    other = next(r for r in ROLES if r != role)
    with pytest.raises(InvalidResult, match="not examined"):
        parse_review(_review(other), role)


def test_an_unknown_role_has_no_checklist() -> None:
    with pytest.raises(InvalidResult, match="no checklist"):
        parse_review(_review("code_reviewer"), "planner")


@pytest.mark.parametrize("role", ROLES)
def test_the_schema_requires_exactly_the_roles_items(role: str) -> None:
    checklist = review_json_schema(role)["properties"]["checklist"]
    assert checklist["required"] == list(REVIEW_CHECKLISTS[role])
    assert set(checklist["properties"]) == set(REVIEW_CHECKLISTS[role])
    assert checklist["additionalProperties"] is False
    assert "checklist" in review_json_schema(role)["required"]


@pytest.mark.parametrize("role", ROLES)
def test_the_prompt_names_every_item(role: str) -> None:
    text = checklist_text(role)
    assert all(f"- {item}:" in text for item in REVIEW_CHECKLISTS[role])
