"""Phase 5 T4: timers (ADR-018). Schedules request plans; follow-ups remind once; nothing fires twice.

Time is moved by putting `due_at` in the past, never by sleeping. Every state change goes through
the services, and the invariants (including "a pending follow-up still has something to wait on")
are checked on teardown.
"""

import threading
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from axiom.config import get_settings
from axiom.domain.plan_machine import PlanActorNotPermitted
from axiom.domain.roles import Actor, ActorKind
from axiom.domain.timer_machine import IllegalTimerTransition
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.orchestrator import plans, timers
from axiom.orchestrator.reaper import Reaper
from axiom.registry.projects import set_test_paths
from axiom.state.db import Conn, connect
from tests.integration.helpers import HUMAN, C, S, do, drive_to
from tests.integration.test_plans import EM, GOOD

AGENT = Actor(ActorKind.AGENT, "DEV-01")
SYSTEM = Actor(ActorKind.SYSTEM, "someone")


def _schedule(db: Conn, every_hours: int = 24, **kw: Any) -> int:
    args: dict[str, Any] = {"project_key": "AX", "specification": "Update the dependencies.",
                            "budget_usd": Decimal("5"), "every_hours": every_hours} | kw  # fmt: skip
    return int(timers.create_schedule(db, HUMAN, **args)["id"])


def _due(db: Conn, timer_id: int, seconds_ago: float = 1) -> None:
    """Time passes: the timer came due `seconds_ago`."""
    db.execute("UPDATE timers SET due_at = now() - make_interval(secs => %s) WHERE id = %s",
               (seconds_ago, timer_id))  # fmt: skip


def _timer(db: Conn, timer_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM timers WHERE id = %s", (timer_id,)).fetchone()
    assert row is not None
    return row


def _events(db: Conn, timer_id: int) -> list[str]:
    rows = db.execute("SELECT type FROM events WHERE timer_id = %s ORDER BY id", (timer_id,))
    return [r["type"] for r in rows.fetchall()]


def _count(db: Conn, table: str) -> int:
    row = db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # test-only, fixed names
    assert row is not None
    return int(row["n"])


def _follow_ups(db: Conn, status: str = "PENDING") -> list[dict[str, Any]]:
    return db.execute(
        "SELECT * FROM timers WHERE kind = 'follow_up' AND status = %s ORDER BY id", (status,)
    ).fetchall()


# ---- schedules -------------------------------------------------------------------------------


def test_a_human_sets_up_a_schedule(org: Conn) -> None:
    timer_id = _schedule(org)
    t = _timer(org, timer_id)
    assert (t["kind"], t["status"], t["every_seconds"], t["created_by"]) == (
        "schedule", "PENDING", 86_400, "human:tester")  # fmt: skip
    assert _events(org, timer_id) == ["TIMER_CREATED"]
    assert timers.fire_due_timers(org) == []  # not due for a day


@pytest.mark.parametrize("actor", [AGENT, SYSTEM], ids=str)
def test_only_a_human_sets_up_a_schedule(org: Conn, actor: Actor) -> None:
    before = (_count(org, "timers"), _count(org, "events"))
    with pytest.raises(Forbidden):
        timers.create_schedule(org, actor, project_key="AX", specification="x",
                               budget_usd=Decimal(1), every_hours=24)  # fmt: skip
    assert (_count(org, "timers"), _count(org, "events")) == before


@pytest.mark.parametrize(
    ("kw", "error"),
    [({"specification": " "}, InvalidInput), ({"budget_usd": Decimal(0)}, InvalidInput),
     ({"every_hours": 0}, InvalidInput), ({"every_hours": 24 * 91}, InvalidInput),
     ({"project_key": "NOPE"}, NotFound)],
    ids=["no spec", "no budget", "too often", "too rare", "no project"],
)  # fmt: skip
def test_a_bad_schedule_creates_nothing(org: Conn, kw: dict[str, Any], error: type) -> None:
    before = (_count(org, "timers"), _count(org, "events"))
    with pytest.raises(error):
        _schedule(org, **kw)
    assert (_count(org, "timers"), _count(org, "events")) == before


def test_the_database_refuses_a_timer_from_an_agent(org: Conn) -> None:
    project = org.execute("SELECT id FROM projects WHERE key = 'AX'").fetchone()
    assert project is not None
    # Refused by the shape check or the agent check, whichever Postgres evaluates first.
    with pytest.raises(psycopg.errors.CheckViolation, match="timers"):
        org.execute(
            """INSERT INTO timers (kind, status, project_id, due_at, every_seconds, specification,
                                   budget_usd, created_by)
               VALUES ('schedule', 'PENDING', %s, now(), 3600, 'x', 1, 'agent:DEV-01')""",
            (project["id"],),
        )


def test_a_due_schedule_requests_a_plan_that_still_needs_you(org: Conn) -> None:
    timer_id = _schedule(org, budget_usd=Decimal(10))
    _due(org, timer_id)
    assert timers.fire_due_timers(org) == [timer_id]
    t = _timer(org, timer_id)
    assert (t["status"], t["fired_count"]) == ("PENDING", 1)
    plan = org.execute("SELECT * FROM plans WHERE timer_id = %s", (timer_id,)).fetchone()
    assert plan is not None
    assert (plan["status"], plan["created_by"], plan["specification"]) == (
        "REQUESTED", "system:scheduler", "Update the dependencies.")  # fmt: skip
    assert _events(org, timer_id)[:3] == ["TIMER_CREATED", "PLAN_REQUESTED", "TIMER_FIRED"]
    nxt = org.execute("SELECT %s > now() AS later", (t["due_at"],)).fetchone()
    assert nxt is not None and nxt["later"]
    # The scheduler can't approve what it asked for: only a human approves a plan.
    set_test_paths(org, HUMAN, "AX", ["agent/tests/**"])
    assert plans.claim_plan(org, EM) is not None
    assert plans.submit_plan(org, EM, plan["id"], GOOD, {"cost": {"usd": "0.40"}}) == "PROPOSED"
    with pytest.raises(PlanActorNotPermitted):
        plans.approve_plan(org, timers.SCHEDULER, plan["id"])
    assert _count(org, "tasks") == 0


def test_a_schedule_missed_while_axiom_was_down_fires_once(org: Conn) -> None:
    """Down for three and a half days on a daily schedule: one plan, not four, and the next one
    stays on the schedule's own times."""
    timer_id = _schedule(org)
    _due(org, timer_id, seconds_ago=3.5 * 86_400)
    original = _timer(org, timer_id)["due_at"]
    assert timers.fire_due_timers(org) == [timer_id]
    assert timers.fire_due_timers(org) == []
    t = _timer(org, timer_id)
    assert (t["due_at"] - original).total_seconds() == 4 * 86_400
    assert _count(org, "plans") == 1
    fired = org.execute(
        "SELECT payload FROM events WHERE timer_id = %s AND type = 'TIMER_FIRED'", (timer_id,)
    ).fetchone()
    assert fired is not None and fired["payload"]["missed"] == 3


def test_two_schedulers_never_fire_the_same_timer(org: Conn, db_url: str) -> None:
    """Two API processes sweeping at the same moment: each due timer fires exactly once."""
    ids = [_schedule(org) for _ in range(6)]
    for timer_id in ids:
        _due(org, timer_id)
    fired: list[list[int]] = []
    barrier = threading.Barrier(2)

    def sweep() -> None:
        with connect(db_url) as conn:
            barrier.wait()
            fired.append(timers.fire_due_timers(conn))

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(fired[0] + fired[1]) == ids
    assert _count(org, "plans") == 6


def test_a_schedule_the_day_cant_pay_for_is_refused_and_recorded(
    org: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "daily_budget_usd", Decimal("0.40"))  # under a plan's $0.50
    timer_id = _schedule(org)
    _due(org, timer_id)
    assert timers.fire_due_timers(org) == [timer_id]
    assert _count(org, "plans") == 0
    t = _timer(org, timer_id)
    assert (t["status"], t["fired_count"]) == ("PENDING", 1)  # it'll try again next time
    fired = org.execute(
        "SELECT payload FROM events WHERE timer_id = %s AND type = 'TIMER_FIRED'", (timer_id,)
    ).fetchone()
    assert fired is not None and "day's budget" in fired["payload"]["refused"]


def test_a_human_cancels_a_schedule(org: Conn) -> None:
    timer_id = _schedule(org)
    timers.cancel_timer(org, HUMAN, timer_id)
    t = _timer(org, timer_id)
    assert (t["status"], t["ended_by"]) == ("CANCELLED", "human:tester")
    _due(org, timer_id)
    assert timers.fire_due_timers(org) == []
    assert _events(org, timer_id) == ["TIMER_CREATED", "TIMER_CANCELLED"]


def test_a_cancelled_timer_cant_be_cancelled_again_or_by_an_agent(org: Conn) -> None:
    timer_id = _schedule(org)
    with pytest.raises(Exception, match="may not cancel"):
        timers.cancel_timer(org, AGENT, timer_id)
    assert _timer(org, timer_id)["status"] == "PENDING"
    timers.cancel_timer(org, HUMAN, timer_id)
    before, events = _timer(org, timer_id), _count(org, "events")
    with pytest.raises(IllegalTimerTransition):
        timers.cancel_timer(org, HUMAN, timer_id)
    assert _timer(org, timer_id) == before and _count(org, "events") == events


def test_the_reaper_fires_timers_on_its_sweep(org: Conn, db_url: str) -> None:
    """The reaper's startup sweep is what fires timers that came due while Axiom was down."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    timer_id = _schedule(org)
    _due(org, timer_id)
    with ConnectionPool(db_url, kwargs={"autocommit": True, "row_factory": dict_row},
                        min_size=1, max_size=1) as pool:  # fmt: skip
        assert f"timer:{timer_id}" in Reaper(pool, 60).sweep()  # type: ignore[arg-type]


# ---- follow-ups ------------------------------------------------------------------------------


def test_a_question_gets_one_reminder_and_the_answer_clears_it(org: Conn) -> None:
    key = drive_to(org, S.WAITING_FOR_HUMAN)
    [follow_up] = _follow_ups(org)
    assert (follow_up["reason"], follow_up["subject_state"], follow_up["created_by"]) == (
        "waiting_for_human", "WAITING_FOR_HUMAN", "system:follow-ups")  # fmt: skip
    do(org, key, C.ANSWER)
    assert _follow_ups(org) == []
    assert _timer(org, follow_up["id"])["ended_by"] == "system:follow-ups"


def test_a_follow_up_fires_once_if_you_havent_acted(org: Conn) -> None:
    drive_to(org, S.WAITING_FOR_HUMAN)
    [follow_up] = _follow_ups(org)
    _due(org, follow_up["id"])
    assert timers.fire_due_timers(org) == [follow_up["id"]]
    assert timers.fire_due_timers(org) == []
    assert _timer(org, follow_up["id"])["status"] == "FIRED"
    assert _events(org, follow_up["id"]) == ["TIMER_CREATED", "TIMER_FIRED"]


def test_a_pr_waiting_for_your_merge_gets_a_reminder_until_you_merge(org: Conn) -> None:
    key = drive_to(org, S.AWAITING_MERGE)
    [follow_up] = _follow_ups(org)
    assert follow_up["reason"] == "awaiting_merge"
    due = org.execute("SELECT due_at - created_at AS wait FROM timers WHERE id = %s",
                      (follow_up["id"],)).fetchone()  # fmt: skip
    assert due is not None and due["wait"].total_seconds() == 2 * 86_400
    do(org, key, C.MERGE)
    assert _follow_ups(org) == []


def test_cancelling_a_waiting_task_clears_its_reminder(org: Conn) -> None:
    key = drive_to(org, S.WAITING_FOR_HUMAN)
    do(org, key, C.CANCEL)
    assert _follow_ups(org) == []


def test_work_that_doesnt_wait_for_you_sets_no_follow_up(org: Conn) -> None:
    drive_to(org, S.IN_REVIEW)
    assert _follow_ups(org) == []


def test_a_proposed_plan_gets_a_reminder_until_you_decide(org: Conn) -> None:
    set_test_paths(org, HUMAN, "AX", ["agent/tests/**"])
    plan_id = int(plans.request_plan(org, HUMAN, project_key="AX", specification="Build it.",
                                     budget_usd=Decimal(10))["id"])  # fmt: skip
    assert plans.claim_plan(org, EM) is not None
    assert _follow_ups(org) == []  # requested and claimed plans don't wait for you
    plans.submit_plan(org, EM, plan_id, GOOD, {"cost": {"usd": "0.40"}})
    [follow_up] = _follow_ups(org)
    assert (follow_up["plan_id"], follow_up["reason"]) == (plan_id, "plan_proposed")
    plans.approve_plan(org, HUMAN, plan_id)
    assert _follow_ups(org) == []


def test_timer_events_stay_out_of_the_tasks_own_history(org: Conn) -> None:
    key = drive_to(org, S.WAITING_FOR_HUMAN)
    rows = org.execute(
        "SELECT type, timer_id, task_id, payload FROM events WHERE type LIKE 'TIMER_%'"
    ).fetchall()
    assert [r["type"] for r in rows] == ["TIMER_CREATED"]
    task = org.execute("SELECT id FROM tasks WHERE key = %s", (key,)).fetchone()
    assert task is not None
    assert rows[0]["task_id"] is None and rows[0]["payload"]["task_id"] == task["id"]
