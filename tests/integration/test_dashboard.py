"""Phase 4 T2: the report and the read-only dashboard: login, content, escaping, no writes."""

import tomllib
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from axiom.api.app import create_app
from axiom.api.ui import ui_router
from axiom.config import Settings
from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.orchestrator.reviews import claim_review, submit_review
from axiom.report import build_report, fmt_duration
from axiom.state.db import Conn
from tests.conftest import TEST_TOKENS
from tests.integration.helpers import do, drive_to, new_task
from tests.integration.test_reviews import BUG, FAIL, REV, _in_review

C = TaskCommand
EVIL = '<script>alert("x")</script>'


@pytest.fixture
def browser(api_tokens: Conn, db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(database_url=db_url), start_reaper=False)) as c:
        yield c


def _login(c: TestClient) -> None:
    r = c.post("/ui/login", data={"token": TEST_TOKENS["human:bharath"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_pages_need_a_login(browser: TestClient) -> None:
    for path in ("/ui", "/ui/tasks/AX-001"):
        r = browser.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/ui/login"
    assert browser.post("/ui/login", data={"token": "axm_wrong"}).status_code == 401
    assert "API token" in browser.get("/ui/login").text


def test_overview_shows_what_needs_you_and_the_numbers(
    browser: TestClient, api_tokens: Conn
) -> None:
    key = drive_to(api_tokens, TaskState.AWAITING_MERGE)
    _login(browser)
    page = browser.get("/ui").text
    assert "Needs you" in page and key in page and "merge it" in page
    assert "Cost per merged task" in page and "upper bound on attention" in page


def test_demo_tasks_are_hidden_unless_asked(browser: TestClient, api_tokens: Conn) -> None:
    key = new_task(api_tokens).key
    api_tokens.execute("""UPDATE tasks SET tags = '["demo"]' WHERE key = %s""", (key,))
    _login(browser)
    assert key not in browser.get("/ui").text
    assert key in browser.get("/ui", params={"include_demo": True}).text


def test_task_page_shows_reviews_runs_and_timeline(browser: TestClient, api_tokens: Conn) -> None:
    key = _in_review(api_tokens)
    claimed = claim_review(api_tokens, REV)
    assert claimed is not None
    submit_review(api_tokens, REV, claimed.review_id, FAIL, {"cost": {"usd": "0.26"}})
    _login(browser)
    page = browser.get(f"/ui/tasks/{key}").text
    assert BUG["issue"] in page and "$0.26" in page
    assert "TASK_REOPENED" in page and "system:aggregator" in page and "Developer runs" in page


def test_untrusted_text_is_escaped_everywhere(browser: TestClient, api_tokens: Conn) -> None:
    """Task text, review findings and errors come from models and repositories."""
    key = new_task(api_tokens, title=EVIL, specification=EVIL, acceptance_criteria=[EVIL]).key
    do(api_tokens, key, C.ASSIGN)
    do(api_tokens, key, C.START)
    do(api_tokens, key, C.SUBMIT, payload={"commit": "a", "pr_url": 'javascript:alert("pr")'})
    claimed = claim_review(api_tokens, REV)
    assert claimed is not None
    evil_finding = BUG | {"issue": EVIL, "required_change": EVIL, "file": EVIL}
    submit_review(
        api_tokens,
        REV,
        claimed.review_id,
        {"verdict": "FAIL", "summary": EVIL, "findings": [evil_finding]},
    )
    _login(browser)
    for path in ("/ui", f"/ui/tasks/{key}"):
        page = browser.get(path).text
        assert "<script>alert" not in page
        assert 'href="javascript:' not in page and "href='javascript:" not in page
    assert "&lt;script&gt;" in browser.get(f"/ui/tasks/{key}").text


def test_a_task_without_a_pr_shows_no_pr_link(browser: TestClient, api_tokens: Conn) -> None:
    key = new_task(api_tokens).key
    _login(browser)
    assert "<td>PR</td>" not in browser.get("/ui").text
    assert "Pull request" not in browser.get(f"/ui/tasks/{key}").text


def test_unknown_task_is_404(browser: TestClient) -> None:
    _login(browser)
    assert browser.get("/ui/tasks/AX-999").status_code == 404


def test_only_login_is_not_a_get() -> None:
    """Read from the router: FastAPI 0.162's app.routes wraps it, and would make this vacuous."""
    routes = [r for r in ui_router.routes if isinstance(r, APIRoute)]
    assert {r.path for r in routes} == {"/ui", "/ui/login", "/ui/tasks/{key}"}
    assert {r.path for r in routes if r.methods - {"GET", "HEAD"}} == {"/ui/login"}


def test_report(api_tokens: Conn) -> None:
    drive_to(api_tokens, TaskState.AWAITING_MERGE)
    report = build_report(api_tokens, "AX")
    assert report.startswith("# Axiom daily report: AX")
    assert "**AX-001** is AWAITING_MERGE" in report
    assert "of $15.00" in report and "upper bound on attention" in report
    assert "without these, the main question can't be answered" in report


def test_report_with_nothing_to_do(org: Conn) -> None:
    report = build_report(org)
    assert "## Needs you\n\nNothing." in report and "Cost per merged task (all spend): -" in report


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "-"), (59, "0m 59s"), (125, "2m 05s"), (4253, "1h 10m")]
)
def test_fmt_duration(seconds: float | None, text: str) -> None:
    assert fmt_duration(seconds) == text


def test_form_parsing_is_a_runtime_dependency() -> None:
    """The image installs with --no-dev; without this the API can't start, though tests pass."""
    with open("pyproject.toml", "rb") as f:
        dependencies = tomllib.load(f)["project"]["dependencies"]
    assert any(d.startswith("python-multipart") for d in dependencies)
