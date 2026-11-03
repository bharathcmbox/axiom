"""Budgets are enforced, not just reported (SPEC principle 8).

- Per task: `tasks.budget_usd` caps the summed cost of all its executions.
- Per day: `daily_budget_usd` caps the summed cost of executions started today (UTC).
- Per run: the worker receives `run_budget_usd` = min(task remaining, day remaining) and
  passes it to the runtime as a hard stop.

Claims are refused when either budget is spent. A task whose budget runs out while failing goes
to FAILED instead of back to READY (a READY task that can never be claimed would be stuck).
"""

from decimal import ROUND_DOWN, Decimal
from typing import Any

from axiom.config import get_settings
from axiom.errors import AssignmentRejected, InvalidInput
from axiom.state.db import Conn

ZERO = Decimal("0")
CENT = Decimal("0.01")


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
                  AS s"""
    ).fetchone()
    assert row is not None
    return Decimal(row["s"])


def run_budget(conn: Conn, task_id: int, budget_usd: Decimal | None) -> Decimal:
    """What a new execution of this task may spend: min(task remaining, day remaining)."""
    day_left = get_settings().daily_budget_usd - daily_spent(conn)
    left = day_left if budget_usd is None else min(budget_usd - task_spent(conn, task_id), day_left)
    # Round down to whole cents: never hand out more than is left.
    return max(left, ZERO).quantize(CENT, rounding=ROUND_DOWN)


def check_can_start(conn: Conn, row: dict[str, Any]) -> Decimal:
    """Raise AssignmentRejected if the task or the day has no budget left; else the run budget."""
    cap = get_settings().daily_budget_usd
    if daily_spent(conn) >= cap:
        raise AssignmentRejected(f"daily budget of ${cap} is spent")
    budget = row["budget_usd"]
    if budget is not None and task_spent(conn, row["id"]) >= budget:
        raise AssignmentRejected(f"task {row['key']} has spent its budget of ${budget}")
    return run_budget(conn, row["id"], budget)


def task_budget_exhausted(conn: Conn, task_id: int, budget_usd: Decimal | None) -> bool:
    return budget_usd is not None and task_spent(conn, task_id) >= budget_usd


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
