"""Structured contracts exchanged with agents (SPEC section 6).

`ResultContract` is what a developer agent returns at the end of a run. It is used twice:
as the JSON schema handed to the model (structured output), and to validate whatever actually
comes back. The worker adds what the agent cannot know or must not be trusted with: verified
test results, branch, PR URL and cost.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Strict(BaseModel):
    # strict: no silent coercion ("yes" is not a bool, "8" is not an int); extra fields rejected.
    model_config = ConfigDict(extra="forbid", strict=True)


class TestsRun(_Strict):
    command: str
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)


class CriterionResult(_Strict):
    criterion: str
    met: bool
    evidence: str = Field(description="Test name, command output line or file reference.")


class ResultContract(_Strict):
    """A developer agent's report. Test results here are claims; the worker re-runs tests."""

    status: Literal["submitted", "needs_input"]
    summary: str = Field(min_length=1, max_length=4000)
    files_changed: list[str] = Field(default_factory=list)
    tests_run: TestsRun | None = None
    criteria: list[CriterionResult] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    question: str | None = Field(
        default=None, description="Required when status is needs_input: one clear question."
    )

    @model_validator(mode="after")
    def _question_iff_needs_input(self) -> Self:
        if (self.status == "needs_input") != bool(self.question and self.question.strip()):
            raise ValueError("question is required exactly when status is needs_input")
        return self


class InvalidResult(Exception):
    """The agent's result did not match the contract."""


def parse_result(data: Any) -> ResultContract:
    try:
        return ResultContract.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise InvalidResult(problems) from None


def result_json_schema() -> dict[str, Any]:
    return ResultContract.model_json_schema()


# ---- review contract (SPEC section 6, ADR-009) --------------------------------------------

BLOCKING_SEVERITIES = ("high", "critical")


class Finding(_Strict):
    severity: Literal["low", "medium", "high", "critical"]
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    issue: str = Field(min_length=1)
    required_change: str = Field(min_length=1)


# What each reviewer role must look at (Phase 5 T2). The prompt is built from this, the model's
# output schema requires every item, and a review that skips one, or approves having examined
# nothing, is invalid: it's retried like any other bad contract.
REVIEW_CHECKLISTS: dict[str, dict[str, str]] = {
    "code_reviewer": {
        "correctness": "does the code do what the specification says, including edge cases (empty "
        "input, boundaries, off-by-one, ordering, time zones, None)?",
        "acceptance_criteria": "is each acceptance criterion actually met by the change?",
        "tests": "do the tests exercise the new behaviour, and would they catch the obvious bug?",
        "scope": "only the allowed paths changed; no unrelated changes; no dead code.",
        "maintainability": "clear names, no needless complexity.",
    },
    "security_reviewer": {
        "injection": "shell, SQL, path traversal, template, prompt injection into LLM calls.",
        "secrets": "credentials or tokens in code, config or logs; secrets in error messages.",
        "authorization": "untrusted input reaching privileged actions; missing checks.",
        "unsafe_execution": "eval/exec, subprocess with shell=True, deserializing untrusted data.",
        "dependencies": "new packages, unpinned or suspicious; supply-chain risk.",
        "data_exposure": "sensitive data logged, returned or sent to external services.",
    },
}


class ChecklistEntry(_Strict):
    result: Literal["ok", "finding", "not_applicable"]
    note: str = Field(min_length=1, max_length=500, description="What was looked at, briefly.")


class ReviewContract(_Strict):
    """A reviewer's verdict on one round of work."""

    verdict: Literal["PASS", "FAIL"]
    summary: str = Field(min_length=1, max_length=4000)
    checklist: dict[str, ChecklistEntry]
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING_SEVERITIES]


class PlanTask(_Strict):
    """One task a planner proposes. It grants nothing until a human approves the plan."""

    ref: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    title: str = Field(min_length=1, max_length=120)
    specification: str = Field(min_length=1, max_length=8000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    role: Literal["developer", "qa"]
    allowed_paths: list[str] = Field(default_factory=list, max_length=30)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    budget_usd: float = Field(gt=0, le=50)
    depends_on: list[str] = Field(default_factory=list)


class PlanContract(_Strict):
    """A planner's proposal (ADR-011). Its rules are in orchestrator/plan_rules.py."""

    summary: str = Field(min_length=1, max_length=4000)
    tasks: list[PlanTask]
    risks: list[str] = Field(default_factory=list)


def parse_review(data: Any, role: str) -> ReviewContract:
    """Validate a review against the contract and the checklist for `role`."""
    try:
        contract = ReviewContract.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise InvalidResult(problems) from None
    gaps = checklist_problems(contract, role)
    if gaps:
        raise InvalidResult("; ".join(gaps))
    return contract


def checklist_problems(contract: ReviewContract, role: str) -> list[str]:
    items = REVIEW_CHECKLISTS.get(role)
    if items is None:
        return [f"no checklist for role {role!r}"]
    problems = []
    missing = [i for i in items if i not in contract.checklist]
    unknown = sorted(set(contract.checklist) - set(items))
    if missing:
        problems.append(f"checklist: not examined: {', '.join(missing)}")
    if unknown:
        problems.append(f"checklist: not items for {role}: {', '.join(unknown)}")
    results = [e.result for e in contract.checklist.values()]
    if contract.verdict == "PASS" and results and all(r == "not_applicable" for r in results):
        problems.append("checklist: a PASS that examined nothing (every item not_applicable)")
    if "finding" in results and not contract.findings:
        problems.append("checklist: an item has a finding, but findings is empty")
    return problems


def review_json_schema(role: str) -> dict[str, Any]:
    """The contract's schema, with the checklist narrowed to `role`'s items, all required."""
    schema = ReviewContract.model_json_schema()
    items = REVIEW_CHECKLISTS[role]
    entry = schema["properties"]["checklist"]["additionalProperties"]
    schema["properties"]["checklist"] = {
        "type": "object",
        "properties": {i: entry for i in items},
        "required": list(items),
        "additionalProperties": False,
    }
    return schema


def plan_json_schema() -> dict[str, Any]:
    return PlanContract.model_json_schema()


def parse_plan(data: Any) -> PlanContract:
    try:
        return PlanContract.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]
        )
        raise InvalidResult(problems) from None
