"""Budgets are enforced, not just reported (SPEC principle 8).

- Per task: `tasks.budget_usd` caps the summed cost of all its executions.
- Per day: `daily_budget_usd` caps the summed cost of executions started today (UTC).
- Per run: the worker receives `run_budget_usd` = min(per-run cap, task left, day left) and
  passes it to the runtime as a hard stop.

Claims are refused when either budget is spent. A task whose budget runs out while failing goes
to FAILED instead of back to READY (a READY task that can never be claimed would be stuck).

Costs are only known when a run ends, so a claim also **reserves** its run budget: "left" means
budget minus spent minus what active runs may still spend. BUDGET_LOCK_ID serializes that
check-and-reserve. A lock without reservations would achieve nothing, because a claim doesn't
change what was spent: two serialized claims would see the same money and both spend it.
"""

from decimal import ROUND_DOWN, Decimal
from typing import Any

from axiom.config import get_settings
from axiom.errors import AssignmentRejected, InvalidInput, TooLittleBudget
from axiom.state.db import Conn

ZERO = Decimal("0")
CENT = Decimal("0.01")
BUDGET_LOCK_ID = 727_001  # lock order: task -> agent -> developer cap -> this


def task_spent(conn: Conn, task_id: int) -> Decimal:
    """Developer executions plus reviews: the full cost of delivering the task."""
    row = conn.execute(
        """SELECT (SELECT coalesce(sum(cost_usd), 0) FROM executions WHERE task_id = %(t)s)
                + (SELECT coalesce(sum(cost_usd), 0) FROM reviews WHERE task_id = %(t)s) AS s""",
        {"t": task_id},
    ).fetchone()
    assert row is not None
    return Decimal(row["s"])


def daily_spent(conn: Conn) -> Decimal:
    row = conn.execute(
        """WITH day AS (SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS d)
           SELECT (SELECT coalesce(sum(cost_usd), 0) FROM executions, day WHERE started_at >= day.d)
                + (SELECT coalesce(sum(cost_usd), 0) FROM reviews, day WHERE created_at >= day.d)
                + (SELECT coalesce(sum(cost_usd), 0) FROM plans, day WHERE created_at >= day.d)
                  AS s"""
    ).fetchone()
    assert row is not None
    return Decimal(row["s"])


def reserved(conn: Conn, task_id: int | None = None) -> Decimal:
    """What active runs may still spend: all of them, or one task's."""
    row = conn.execute(
        """SELECT (SELECT coalesce(sum(run_budget_usd), 0) FROM executions
                   WHERE status IN ('STARTING', 'RUNNING')
                     AND (%(t)s::bigint IS NULL OR task_id = %(t)s))
                + (SELECT coalesce(sum(run_budget_usd), 0) FROM reviews
                   WHERE status = 'CLAIMED'
                     AND (%(t)s::bigint IS NULL OR task_id = %(t)s))
                + (SELECT coalesce(sum(run_budget_usd), 0) FROM plans
                   WHERE status = 'CLAIMED' AND %(t)s::bigint IS NULL) AS s""",
        {"t": task_id},
    ).fetchone()
    assert row is not None
    return Decimal(row["s"])


def available(conn: Conn, task_id: int, budget_usd: Decimal | None, run_cap: Decimal) -> Decimal:
    """What one new run may spend now, rounded down so it never gets more than is left."""
    left = min(run_cap, get_settings().daily_budget_usd - daily_spent(conn) - reserved(conn))
    if budget_usd is not None:
        left = min(left, budget_usd - task_spent(conn, task_id) - reserved(conn, task_id))
    return max(left, ZERO).quantize(CENT, rounding=ROUND_DOWN)


def run_budget(conn: Conn, task_id: int, budget_usd: Decimal | None) -> Decimal:
    """A task's run budget: its active run's reservation, or what a new run would get."""
    active = conn.execute(
        """SELECT run_budget_usd FROM executions
           WHERE task_id = %s AND status IN ('STARTING', 'RUNNING')""",
        (task_id,),
    ).fetchone()
    if active is not None and active["run_budget_usd"] is not None:
        return Decimal(active["run_budget_usd"])
    return available(conn, task_id, budget_usd, get_settings().max_run_budget_usd)


def reserve(
    conn: Conn, row: dict[str, Any], run_cap: Decimal, *, minimum: Decimal = ZERO
) -> Decimal:
    """Check budgets and return what the new run may spend, or raise AssignmentRejected. The
    caller stores the result on the run in this transaction, while the lock is held.

    Below `minimum` a run would burn money and fail (SOC-005 paid $1.11 for that). If what's
    missing is only reserved, it may come back, so the claim waits. If the task simply hasn't got
    it, waiting can't help: TooLittleBudget, before anything is spent."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK_ID,))
    cap = get_settings().daily_budget_usd
    if daily_spent(conn) >= cap:
        raise AssignmentRejected(f"daily budget of ${cap} is spent")
    budget = row["budget_usd"]
    if budget is not None and task_spent(conn, row["id"]) >= budget:
        raise AssignmentRejected(f"task {row['key']} has spent its budget of ${budget}")
    run = available(conn, row["id"], budget, run_cap)
    if run <= ZERO or run < minimum:
        if budget is not None and minimum > ZERO:
            left = budget - task_spent(conn, row["id"])
            if left < minimum and reserved(conn, row["id"]) == ZERO:
                raise TooLittleBudget(
                    f"task {row['key']} has ${left} left of its ${budget}, "
                    f"and a run needs ${minimum}"
                )
        raise AssignmentRejected(
            f"no budget free for {row['key']}: what is left is reserved by runs in flight"
        )
    return run


def reserve_from_the_day(conn: Conn, run_cap: Decimal, *, minimum: Decimal = ZERO) -> Decimal:
    """Like `reserve`, for work that belongs to no task: a planning run."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUDGET_LOCK_ID,))
    cap = get_settings().daily_budget_usd
    if daily_spent(conn) >= cap:
        raise AssignmentRejected(f"daily budget of ${cap} is spent")
    left = min(run_cap, cap - daily_spent(conn) - reserved(conn))
    run = max(left, ZERO).quantize(CENT, rounding=ROUND_DOWN)
    if run <= ZERO or run < minimum:
        raise AssignmentRejected("no budget free: what is left is reserved by runs in flight")
    return run


def check_can_start(conn: Conn, row: dict[str, Any]) -> Decimal:
    return reserve(conn, row, get_settings().max_run_budget_usd)


def task_budget_exhausted(conn: Conn, task_id: int, budget_usd: Decimal | None) -> bool:
    return budget_usd is not None and task_spent(conn, task_id) >= budget_usd


def is_estimated(payload: dict[str, Any]) -> bool:
    cost = payload.get("cost")
    return isinstance(cost, dict) and cost.get("estimated") is True


def parse_cost(payload: dict[str, Any]) -> tuple[Decimal | None, int | None, int | None]:
    """Read the optional `cost` object a worker reports: {usd, input_tokens, output_tokens}."""
    cost = payload.get("cost")
    if cost is None:
        return None, None, None
    if not isinstance(cost, dict):
        raise InvalidInput("cost must be an object {usd, input_tokens, output_tokens}")
    try:
        usd = Decimal(str(cost["usd"])) if cost.get("usd") is not None else None
        tokens_in = int(cost["input_tokens"]) if cost.get("input_tokens") is not None else None
        tokens_out = int(cost["output_tokens"]) if cost.get("output_tokens") is not None else None
    except ArithmeticError, TypeError, ValueError:
        raise InvalidInput("cost values must be numbers") from None
    if any(v is not None and v < 0 for v in (usd, tokens_in, tokens_out)):
        raise InvalidInput("cost values must be >= 0")
    return usd, tokens_in, tokens_out
