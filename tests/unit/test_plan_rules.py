"""Phase 4 T6: the plan contract and validation rules. Each rule, passing and failing."""

from decimal import Decimal
from typing import Any

import pytest

from axiom.contracts import InvalidResult, PlanContract, parse_plan
from axiom.orchestrator.plan_rules import (
    MAX_TASKS,
    has_cycle,
    may_overlap,
    validate_plan,
    within_test_paths,
)

TESTS = ["agent/tests/**"]


def _task(ref: str = "a", **kwargs: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "title": f"Task {ref}",
        "specification": "Do it.",
        "acceptance_criteria": ["It's done."],
        "role": "developer",
        "allowed_paths": [f"agent/{ref}.py"],
        "budget_usd": 1.0,
        "depends_on": [],
    } | kwargs


def _plan(*tasks: dict[str, Any]) -> PlanContract:
    return parse_plan({"summary": "A plan.", "tasks": list(tasks), "risks": []})


def _problems(*tasks: dict[str, Any], budget: str = "10", test_paths: Any = None) -> list[str]:
    return validate_plan(
        _plan(*tasks),
        budget_usd=Decimal(budget),
        test_paths=TESTS if test_paths is None else test_paths,
    )


def test_a_sound_plan_has_no_problems() -> None:
    assert (
        _problems(
            _task("impl", allowed_paths=["agent/socagent/incidents.py"]),
            _task("cli", allowed_paths=["agent/socagent/__main__.py"]),
            _task("qa", role="qa", allowed_paths=["agent/tests/**"], depends_on=["impl", "cli"]),
        )
        == []
    )


# ---- the contract ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"ref": "Has Spaces"},
        {"ref": "../evil"},
        {"role": "engineering_manager"},
        {"role": "code_reviewer"},  # a plan can't create its own reviewers
        {"budget_usd": 0},
        {"budget_usd": -1},
        {"budget_usd": "3.00"},  # strict: no silent coercion
        {"budget_usd": 51},
        {"acceptance_criteria": []},
        {"priority": "P9"},
        {"title": ""},
        {"approved": True},  # an unknown field isn't ignored
        {"assigned_agent": "DEV-01"},
    ],
)
def test_the_contract_refuses_anything_off_shape(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidResult):
        _plan(_task(**bad))


@pytest.mark.parametrize(
    "bad",
    [
        {"tasks": []},  # no summary
        {"summary": "s"},  # no tasks
        {"summary": "", "tasks": []},
        {"summary": "s", "tasks": [], "extra": 1},
    ],
)
def test_the_plan_itself_has_a_strict_shape(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidResult):
        parse_plan(bad)


def test_an_empty_plan_is_refused() -> None:
    assert validate_plan(
        parse_plan({"summary": "s", "tasks": []}), budget_usd=Decimal(1), test_paths=TESTS
    ) == ["a plan needs at least one task"]


# ---- dependencies ---------------------------------------------------------------------------


def test_an_unknown_dependency_is_refused() -> None:
    assert any("isn't in the plan" in p for p in _problems(_task("a", depends_on=["ghost"])))


def test_a_task_cannot_depend_on_itself() -> None:
    assert any("depends on itself" in p for p in _problems(_task("a", depends_on=["a"])))


@pytest.mark.parametrize(
    "edges",
    [
        {"a": ["b"], "b": ["a"]},
        {"a": ["b"], "b": ["c"], "c": ["a"]},
        {"a": ["b"], "b": ["c"], "c": ["b"]},  # a cycle not reachable from every task
    ],
)
def test_a_cycle_is_refused(edges: dict[str, list[str]]) -> None:
    tasks = [_task(ref, depends_on=deps) for ref, deps in edges.items()]
    assert has_cycle(_plan(*tasks).tasks)
    assert any("cycle" in p for p in _problems(*tasks))


def test_a_diamond_is_not_a_cycle() -> None:
    tasks = [
        _task("a"),
        _task("b", depends_on=["a"]),
        _task("c", depends_on=["a"]),
        _task("d", depends_on=["b", "c"]),
    ]
    assert not has_cycle(_plan(*tasks).tasks)
    assert _problems(*tasks) == []


def test_duplicate_refs_are_refused() -> None:
    assert any("more than once" in p for p in _problems(_task("a"), _task("a")))


# ---- paths ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "**",
        "*",
        "**/*",
        "*.py",
        "/etc/passwd",
        "../outside.py",
        "agent/../../outside.py",
        "agent/../.github/workflows/ci.yml",
        ".env",
        "agent/.env",
        "secrets/key.pem",
        ".github/workflows/ci.yml",
        ".github/**",
        ".GITHUB/workflows/ci.yml",
        ".git/config",
    ],
)
def test_unsafe_paths_are_refused(path: str) -> None:
    assert any(path in p for p in _problems(_task("a", allowed_paths=[path])))


def test_a_task_needs_at_least_one_path() -> None:
    assert any("no allowed paths" in p for p in _problems(_task("a", allowed_paths=[])))


@pytest.mark.parametrize(
    "path", ["agent/x.py", "agent/**", "agent/", "docs/guide/a.md", "README.md"]
)
def test_ordinary_paths_are_fine(path: str) -> None:
    assert _problems(_task("a", allowed_paths=[path])) == []


# ---- QA stays in the tests ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["agent/tests/**", "agent/tests/", "agent/tests/test_x.py", "agent/tests/sub/*.py"]
)
def test_qa_may_change_tests(path: str) -> None:
    assert _problems(_task("q", role="qa", allowed_paths=[path])) == []


@pytest.mark.parametrize(
    "path", ["agent/socagent/x.py", "agent/**", "agent/tests_extra/x.py", "README.md", "agent/*"]
)
def test_qa_may_not_change_anything_else(path: str) -> None:
    assert any(
        "outside the test paths" in p
        for p in _problems(_task("q", role="qa", allowed_paths=[path]))
    )


def test_without_test_paths_a_qa_task_is_refused() -> None:
    problems = _problems(_task("q", role="qa", allowed_paths=["agent/tests/x.py"]), test_paths=[])
    assert any("outside the test paths" in p for p in problems)


def test_an_exact_test_file_allows_only_itself() -> None:
    assert within_test_paths("t/a.py", ["t/a.py"])
    assert not within_test_paths("t/b.py", ["t/a.py"])
    assert not within_test_paths("t/**", ["t/a.py"])


# ---- tasks that run at the same time --------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("agent/x.py", "agent/x.py"),
        ("agent/**", "agent/x.py"),
        ("agent/", "agent/sub/x.py"),
        ("agent/*.py", "agent/x.py"),
        ("**/x.py", "agent/x.py"),  # unsure, so yes
        ("agent/s*/x.py", "agent/sub/x.py"),
    ],
)
def test_overlapping_paths(a: str, b: str) -> None:
    assert may_overlap(a, b) and may_overlap(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("agent/x.py", "agent/y.py"),
        ("agent/**", "docs/**"),
        ("agent/a/", "agent/b/"),
        ("agent", "agent2/x.py"),
        ("agent/x.py", "agent/x.py.bak"),
        ("agent/x.py", "agent/x.py/y"),  # a file isn't a directory
    ],
)
def test_separate_paths(a: str, b: str) -> None:
    assert not may_overlap(a, b) and not may_overlap(b, a)


def test_parallel_tasks_on_the_same_file_are_refused() -> None:
    problems = _problems(
        _task("a", allowed_paths=["agent/x.py"]), _task("b", allowed_paths=["agent/x.py"])
    )
    assert any("can run at the same time" in p for p in problems)


def test_one_after_the_other_may_share_a_file() -> None:
    assert (
        _problems(
            _task("a", allowed_paths=["agent/x.py"]),
            _task("b", allowed_paths=["agent/x.py"], depends_on=["a"]),
        )
        == []
    )


def test_an_order_through_a_third_task_counts() -> None:
    assert (
        _problems(
            _task("a", allowed_paths=["agent/x.py"]),
            _task("b", allowed_paths=["agent/y.py"], depends_on=["a"]),
            _task("c", allowed_paths=["agent/x.py"], depends_on=["b"]),
        )
        == []
    )


def test_siblings_after_one_task_still_run_at_the_same_time() -> None:
    problems = _problems(
        _task("a", allowed_paths=["agent/y.py"]),
        _task("b", allowed_paths=["agent/x.py"], depends_on=["a"]),
        _task("c", allowed_paths=["agent/x.py"], depends_on=["a"]),
    )
    assert any("b and c" in p for p in problems)


# ---- limits ---------------------------------------------------------------------------------


def test_the_budgets_may_reach_but_not_pass_the_plans() -> None:
    tasks = [_task("a", budget_usd=2.5), _task("b", budget_usd=2.5)]
    assert _problems(*tasks, budget="5") == []
    assert any("over the plan" in p for p in _problems(*tasks, budget="4.99"))


def test_budgets_add_up_without_float_error() -> None:
    tasks = [
        _task(f"t{i}", budget_usd=0.1) for i in range(3)
    ]  # 0.1 x 3 as floats is 0.30000000000000004
    assert _problems(*tasks, budget="0.3") == []


def test_too_many_tasks_are_refused() -> None:
    tasks = [_task(f"t{i}", budget_usd=0.1) for i in range(MAX_TASKS)]
    assert _problems(*tasks) == []
    assert any("at most" in p for p in _problems(*tasks, _task("extra", budget_usd=0.1)))


def test_a_credential_in_the_plans_text_is_refused_and_not_repeated() -> None:
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"
    problems = _problems(_task("a", specification=f"Use {secret}"))
    assert any("credential" in p or "API key" in p for p in problems)
    assert not any(secret in p for p in problems)


def test_every_problem_is_reported_at_once() -> None:
    """So one failed attempt tells the planner everything it has to fix."""
    problems = _problems(
        _task("a", allowed_paths=[".env"], depends_on=["ghost"], budget_usd=50), budget="1"
    )
    assert len(problems) == 3
