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


class ReviewContract(_Strict):
    """A reviewer's verdict on one round of work."""

    verdict: Literal["PASS", "FAIL"]
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING_SEVERITIES]


def parse_review(data: Any) -> ReviewContract:
    try:
        return ReviewContract.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise InvalidResult(problems) from None


def review_json_schema() -> dict[str, Any]:
    return ReviewContract.model_json_schema()
