"""When a worker is killed nobody reports its run's cost. On startup, read what it left behind.

Workspaces of killed workers survive. For each one whose run has finished and whose cost is still
unknown, record an estimate from the transcript. The API keeps the first report only, so running
this twice, or on kept workspaces of runs that did report, changes nothing.
"""

import logging
import re
from pathlib import Path

from axiom.runtimes.estimate import estimate_from_transcript
from axiom.worker.api_client import ApiError, AxiomClient

log = logging.getLogger("axiom.worker")
RUN_DIR = re.compile(r"^(?P<key>.+)-(?P<kind>[er])(?P<id>\d+)$")
ACTIVE_EXECUTION = ("STARTING", "RUNNING")


def estimated_cost(run_dir: Path) -> dict[str, object] | None:
    found = estimate_from_transcript(run_dir / "out" / "transcript.jsonl")
    if found is None:
        return None
    return {
        "usd": str(found.usd),
        "input_tokens": found.input_tokens,
        "output_tokens": found.output_tokens,
        "estimated": True,
    }


def recover_costs(api: AxiomClient, workspaces_root: Path, *, kinds: str) -> int:
    """`kinds`: "e" for a developer worker, "r" for a reviewer. Returns how many were recorded."""
    if not workspaces_root.is_dir():
        return 0
    recorded = 0
    for run_dir in sorted(workspaces_root.iterdir()):
        match = RUN_DIR.match(run_dir.name)
        if match is None or match["kind"] not in kinds:
            continue
        run_id = int(match["id"])
        try:
            if _recover_one(api, run_dir, match["key"], match["kind"], run_id):
                recorded += 1
                log.warning("recorded an estimated cost for %s", run_dir.name)
        except (ApiError, OSError) as e:  # someone else's run, or a task since deleted
            log.info("no cost recovered for %s: %s", run_dir.name, e)
    return recorded


def _recover_one(api: AxiomClient, run_dir: Path, key: str, kind: str, run_id: int) -> bool:
    if kind == "e":
        rows = api.executions(key)
        row = next((x for x in rows if x["id"] == run_id), None)
        if row is None or row["status"] in ACTIVE_EXECUTION or row["cost_usd"] is not None:
            return False
    else:
        rows = api.task_reviews(key)
        row = next((x for x in rows if x["id"] == run_id), None)
        if row is None or row["status"] == "CLAIMED" or row["cost_usd"] is not None:
            return False
    cost = estimated_cost(run_dir)
    if cost is None:
        return False
    if kind == "e":
        api.record_execution_cost(run_id, cost)
    else:
        api.record_review_cost(run_id, cost)
    return True
