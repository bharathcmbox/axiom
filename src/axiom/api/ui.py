"""The dashboard: read-only HTML pages over the same data as the API (Phase 4, no new service).

Every page shows text written by models or found in repositories, so everything is escaped, and
only https:// links are rendered. Pages are GET only; login is the sole POST and changes no task.
"""

import html
from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from axiom.config import get_settings
from axiom.domain.roles import Actor
from axiom.events.log import list_events
from axiom.orchestrator import budgets, measures, plans, reviews
from axiom.orchestrator.tasks import get_task, list_tasks
from axiom.registry.tokens import authenticate
from axiom.report import NEEDS_YOU, PLAN_NEEDS_YOU, fmt_duration, fmt_pct, fmt_usd
from axiom.state.db import Conn

ui_router = APIRouter(prefix="/ui", include_in_schema=False)
COOKIE = "axiom_session"

STYLE = """
body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}
th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #ddd;vertical-align:top}
th{font-weight:600;color:#555}
.cards{display:flex;flex-wrap:wrap;gap:.75rem;margin:1rem 0 1.5rem}
.card{border:1px solid #ddd;border-radius:6px;padding:.5rem .9rem;min-width:9rem}
.card b{display:block;font-size:1.3rem}
.state{font-family:monospace;font-size:.85em;padding:.1rem .35rem;border-radius:3px;background:#eee}
.needs{background:#fff3cd;border:1px solid #e5d28a;border-radius:6px;padding:.6rem 1rem}
.FAIL,.FAILED{background:#f8d7da}.PASS,.DONE{background:#d4edda}
small,.muted{color:#777}pre{white-space:pre-wrap;background:#f7f7f7;padding:.6rem}
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> HTMLResponse:
    nav = '<p><a href="/ui">Overview</a> · <a href="/docs">API</a></p>'
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
        f"<style>{STYLE}</style></head><body>{nav}<h1>{e(title)}</h1>{body}</body></html>"
    )


def safe_link(url: str | None, text: str | None = None) -> str:
    """A link only for https:// URLs; any other URL is plain text, and no URL shows nothing."""
    if not url:
        return ""
    if url.startswith("https://"):
        return f'<a href="{e(url)}">{e(text or url)}</a>'
    return e(url)


def cost_cell(row: dict[str, Any]) -> str:
    """An estimate must not read as a billed amount."""
    cost = fmt_usd(row["cost_usd"])
    return f"~{cost} est." if row["cost_estimated"] else cost


def state_tag(state: Any) -> str:
    return f'<span class="state {e(state)}">{e(state)}</span>'


def get_conn(request: Request) -> Iterator[Conn]:
    with request.app.state.pool.connection() as conn:
        yield conn


def require_login(
    conn: Annotated[Conn, Depends(get_conn)],
    token: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> Actor:
    actor = authenticate(conn, token) if token else None
    if actor is None:
        raise HTTPException(status_code=303, headers={"Location": "/ui/login"})
    return actor


ConnDep = Annotated[Conn, Depends(get_conn)]
UserDep = Annotated[Actor, Depends(require_login)]


@ui_router.get("/login")
def login_form() -> HTMLResponse:
    return page(
        "Axiom: sign in",
        '<form method="post" action="/ui/login"><p>API token: '
        '<input type="password" name="token" size="50" autofocus> '
        '<button>Sign in</button></p></form><p class="muted">Issue one with '
        "<code>docker compose exec api python -m axiom.registry.tokens issue human:you</code></p>",
    )


@ui_router.post("/login", response_model=None)
def login(conn: ConnDep, token: Annotated[str, Form()]) -> RedirectResponse | HTMLResponse:
    if authenticate(conn, token) is None:
        response = page(
            "Axiom: sign in", '<p>Invalid or revoked token. <a href="/ui/login">Try again</a></p>'
        )
        response.status_code = 401
        return response
    redirect = RedirectResponse("/ui", status_code=303)
    redirect.set_cookie(COOKIE, token, httponly=True, samesite="strict")
    return redirect


@ui_router.get("")
def overview(conn: ConnDep, user: UserDep, include_demo: bool = False) -> HTMLResponse:
    per_task, s, baselines = measures.collect(conn, include_demo=include_demo)
    metrics = {m.key: m for m in per_task}
    tasks = [t for t in list_tasks(conn) if t.key in metrics]
    waiting = [t for t in tasks if t.state in NEEDS_YOU]
    needs = "".join(
        f"<li><a href='/ui/tasks/{quote(t.key)}'>{e(t.key)}</a> {state_tag(t.state)} {e(t.title)}: "
        f"{e(NEEDS_YOU[t.state])}</li>"
        for t in waiting
    )
    open_plans = [p for p in plans.list_plans(conn) if p["status"] in PLAN_NEEDS_YOU]
    needs = (
        "".join(
            f"<li><a href='/ui/plans/{p['id']}'>Plan {p['id']}</a> {state_tag(p['status'])} "
            f"{e(p['project_key'])}: {e(PLAN_NEEDS_YOU[p['status']])}</li>"
            for p in open_plans
        )
        + needs
    )
    has_needs = waiting or open_plans
    needs_html = f"<div class='needs'><b>Needs you</b><ul>{needs}</ul></div>" if has_needs else ""
    spent, cap = budgets.daily_spent(conn), get_settings().daily_budget_usd
    cards = [
        ("Spent today", f"{fmt_usd(spent)} of {fmt_usd(cap)}"),
        ("Merged", f"{s.merged} of {s.tasks}"),
        ("Cost per merged task", fmt_usd(s.cost_per_merged_usd)),
        ("First-pass acceptance", fmt_pct(s.first_pass_rate)),
        ("Median cycle", fmt_duration(s.median_cycle_s)),
        ("Median waiting for you*", fmt_duration(s.median_waiting_for_human_s)),
        ("Reviews' share of cost", fmt_pct(s.review_share_of_cost)),
        ("Defects after merge", s.defects_after_merge),
    ]
    cards_html = "".join(f"<div class='card'><b>{e(v)}</b>{e(k)}</div>" for k, v in cards)
    rows = "".join(
        f"<tr><td><a href='/ui/tasks/{quote(t.key)}'>{e(t.key)}</a></td>"
        f"<td>{state_tag(t.state)}</td><td>{e(t.title)}</td><td>{fmt_usd(metrics[t.key].cost_usd)}</td>"
        f"<td>{t.review_rounds}</td><td>{safe_link(t.pull_request_url, 'PR')}</td></tr>"
        for t in reversed(tasks)
    )
    toggle = (
        "<a href='/ui'>Hide demo tasks</a>"
        if include_demo
        else "<a href='/ui?include_demo=true'>Show demo tasks</a>"
    )
    return page(
        "Axiom",
        f"{needs_html}<div class='cards'>{cards_html}</div>"
        "<p class='muted'>*Elapsed time: an upper bound on attention, not a measure of it.</p>"
        f"<h2>Tasks</h2><p>{toggle}</p><table><tr><th>Task</th><th>State</th><th>Title</th>"
        f"<th>Cost</th><th>Rework</th><th>PR</th></tr>{rows}</table>"
        f"<h2>Baselines</h2><p>{len(baselines)} recorded.</p>",
    )


@ui_router.get("/tasks/{key}")
def task_page(key: str, conn: ConnDep, user: UserDep) -> HTMLResponse:
    task = get_task(conn, key)
    per_task, _, _ = measures.collect(conn, include_demo=True)
    m = next(x for x in per_task if x.key == key)
    criteria = "".join(f"<li>{e(c)}</li>" for c in task.acceptance_criteria)
    review_rows = "".join(_review_row(r) for r in reviews.list_reviews(conn, task.id))
    executions = conn.execute(
        """SELECT x.*, a.key AS agent_key FROM executions x JOIN agents a ON a.id = x.agent_id
           WHERE x.task_id = %s ORDER BY x.id""",
        (task.id,),
    ).fetchall()
    exec_rows = "".join(
        f"<tr><td>{x['attempt']}</td><td>{e(x['agent_key'])}</td><td>{state_tag(x['status'])}</td>"
        f"<td>{cost_cell(x)}</td><td>{e(x['error'])}</td></tr>"
        for x in executions
    )
    events = "".join(
        f"<tr><td><small>{e(str(ev['occurred_at'])[:19])}</small></td><td>{e(ev['type'])}</td>"
        f"<td>{state_tag(ev['to_state']) if ev['to_state'] else ''}</td>"
        f"<td>{e(ev['actor'])}</td></tr>"
        for ev in list_events(conn, task_id=task.id)
    )
    numbers = asdict(m)
    cards = [
        ("Cost", fmt_usd(m.cost_usd)),
        ("Budget", fmt_usd(task.budget_usd)),
        ("Agents", fmt_duration(numbers["agent_s"])),
        ("Reviews", fmt_duration(numbers["review_s"])),
        ("Waiting for you*", fmt_duration(numbers["waiting_for_human_s"])),
        ("Rework rounds", m.review_rounds),
    ]
    cards_html = "".join(f"<div class='card'><b>{e(v)}</b>{e(k)}</div>" for k, v in cards)
    return page(
        f"{task.key}: {task.title}",
        f"<p>{state_tag(task.state)} {safe_link(task.pull_request_url, 'Pull request')}</p>"
        f"<div class='cards'>{cards_html}</div>"
        f"<h2>Specification</h2><pre>{e(task.specification)}</pre>"
        f"<h2>Acceptance criteria</h2><ul>{criteria}</ul>"
        "<h2>Reviews</h2><table><tr><th>Round</th><th>Role</th><th>Status</th><th>Cost</th>"
        f"<th>Summary and findings</th></tr>{review_rows}</table>"
        "<h2>Developer runs</h2><table><tr><th>#</th><th>Agent</th><th>Status</th><th>Cost</th>"
        f"<th>Error</th></tr>{exec_rows}</table>"
        f"<h2>Timeline</h2><table><tr><th>When</th><th>Event</th><th>State</th><th>Who</th></tr>{events}</table>",
    )


@ui_router.get("/plans/{plan_id}")
def plan_page(plan_id: int, conn: ConnDep, user: UserDep) -> HTMLResponse:
    """Everything needed to decide on a plan. Deciding happens on the API, not here."""
    plan = plans.get_plan(conn, plan_id)
    contract = plan["contract"] or {}
    keys = plan["task_keys"]
    rows = "".join(_plan_task(t, keys) for t in contract.get("tasks", []))
    risks = "".join(f"<li>{e(r)}</li>" for r in contract.get("risks", []))
    feedback = "".join(f"<li>{e(f)}</li>" for f in plan["feedback"])
    tasks = contract.get("tasks", [])
    total = sum(t.get("budget_usd", 0) for t in tasks)
    decide = (
        f"<p>Decide with the API: <code>POST /plans/{plan_id}/approve</code>, "
        "<code>/reject</code> with feedback, or <code>/cancel</code>.</p>"
        if plan["status"] == "PROPOSED"
        else ""
    )
    return page(
        f"Plan {plan_id}: {plan['project_key']}",
        f"<p>{state_tag(plan['status'])} requested by {e(plan['created_by'])}"
        + (f", decided by {e(plan['decided_by'])}" if plan["decided_by"] else "")
        + "</p>"
        f"<div class='cards'><div class='card'><b>{e(f'${total:.2f}')}</b>Tasks' budgets</div>"
        f"<div class='card'><b>{fmt_usd(plan['budget_usd'])}</b>Plan's limit</div>"
        f"<div class='card'><b>{cost_cell(plan)}</b>Planning cost</div>"
        f"<div class='card'><b>{plan['attempt_count']}</b>Failed attempts</div></div>{decide}"
        f"<h2>Specification</h2><pre>{e(plan['specification'])}</pre>"
        + (f"<h2>Error</h2><pre>{e(plan['error'])}</pre>" if plan["error"] else "")
        + (f"<h2>Summary</h2><p>{e(contract.get('summary'))}</p>" if contract else "")
        + (
            "<h2>Proposed tasks</h2><table><tr><th>Ref</th><th>Role</th><th>Title and spec</th>"
            f"<th>May change</th><th>Waits for</th><th>Budget</th></tr>{rows}</table>"
            if rows
            else ""
        )
        + (f"<h2>Risks it names</h2><ul>{risks}</ul>" if risks else "")
        + (f"<h2>Feedback so far</h2><ul>{feedback}</ul>" if feedback else ""),
    )


def _plan_task(t: dict[str, Any], keys: dict[str, str]) -> str:
    ref = t.get("ref", "")
    label = f"<a href='/ui/tasks/{quote(keys[ref])}'>{e(keys[ref])}</a>" if ref in keys else e(ref)
    criteria = "".join(f"<li>{e(c)}</li>" for c in t.get("acceptance_criteria", []))
    paths = "<br>".join(f"<code>{e(p)}</code>" for p in t.get("allowed_paths", []))
    return (
        f"<tr><td>{label}</td><td>{e(t.get('role'))}</td><td><b>{e(t.get('title'))}</b>"
        f"<pre>{e(t.get('specification'))}</pre><ul>{criteria}</ul></td><td>{paths}</td>"
        f"<td>{e(', '.join(t.get('depends_on', [])))}</td><td>{e(t.get('budget_usd'))}</td></tr>"
    )


def _review_row(r: dict[str, Any]) -> str:
    c = r.get("contract") or {}
    findings = "".join(
        f"<li><b>{e(f['severity'])}</b> {e(f.get('file'))}:{e(f.get('line'))} {e(f['issue'])}"
        f"<br><small>→ {e(f['required_change'])}</small></li>"
        for f in c.get("findings", [])
    )
    return (
        f"<tr><td>{r['round']}</td><td>{e(r['role'])}</td><td>{state_tag(r['status'])}</td>"
        f"<td>{cost_cell(r)}</td><td>{e(c.get('summary'))}<ul>{findings}</ul></td></tr>"
    )
