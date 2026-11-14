"""Phase 5 T6: the same past reviews, run on Managed Agents instead of Axiom's own sandbox.

It reuses the comparison drill (cases, snapshots, cap, results file), with `ManagedReviewRuntime`
in place of the sandbox runtime. Like that drill it touches no task or review: results go to a
file. Run it from the admin machine, which holds the Anthropic key for this drill only (ADR-020):

    set -a && . ./.env && set +a
    uv run --extra managed python -m axiom.drills.managed_review \\
        cases.json results.jsonl claude-fable-5-2
"""

import json
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

from axiom.drills.review_compare import Spend, load_cases, run_all, summarize


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import anthropic

    from axiom.runtimes.managed import ManagedReviewRuntime

    cases_file, results_file, *models = argv
    env = os.environ
    if not env.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required for this drill")
    root = Path(env.get("AXIOM_WORKSPACES_DIR", "/tmp/axiom-work")) / "managed"
    root.mkdir(parents=True, exist_ok=True)
    cases = load_cases(Path(cases_file))
    spend = Spend(cap=Decimal(env.get("AXIOM_COMPARE_CAP_USD", "4")))
    client = anthropic.Anthropic()
    ids = root / "managed-ids.json"
    rows = run_all(
        cases,
        models,
        root=root,
        results=Path(results_file),
        runtime_factory=lambda: ManagedReviewRuntime(client, ids_file=ids),
        token=None,  # the repository is public; nothing is sent with a token
        budget=Decimal(env.get("AXIOM_COMPARE_RUN_USD", "1.50")),
        spend=spend,
        parallel=int(env.get("AXIOM_COMPARE_PARALLEL", "1")),
    )
    print(json.dumps(summarize(rows, cases), indent=2))
    if spend.stopped:
        print(f"STOPPED: {spend.stopped}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
