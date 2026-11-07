"""Reviewer comparison (Phase 4 T10b): the same commits reviewed again on another model.

Each case is a commit that real reviewers already judged. The runner rebuilds exactly what they saw
(the commit, and its diff against the base the PR had), and runs the same sandboxed review path on
the model asked for. It never touches tasks or reviews: results go to a JSON-lines file, so the
comparison can't move any task, and every run is still metered by the model proxy (ADR-015).

Run inside a reviewer container, which holds the GitHub token and the proxy and launcher tokens:

    docker compose run --rm -T reviewer-rev-01 python -m axiom.drills.review_compare \
        /work/compare/cases.json /work/compare/results.jsonl claude-sonnet-5-5 claude-fable-5-1
"""

import json
import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.runtimes.base import Outcome
from axiom.runtimes.review import ReviewOutcome, ReviewRequest, ReviewRuntime
from axiom.worker.verify import credentials_in, redact
from axiom.workspaces.git import git, seal

log = logging.getLogger("axiom.drills.review_compare")

_KEY = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MODEL = re.compile(r"^claude-[a-z0-9-]+$")
ROLES = ("code_reviewer", "security_reviewer")


@dataclass(frozen=True)
class Case:
    """One commit, one role, as the original reviewers saw it."""

    task_key: str
    pr: int
    role: str
    repo_url: str
    default_branch: str
    head_sha: str
    base_sha: str
    title: str
    specification: str
    acceptance_criteria: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    developer_result: dict[str, Any] | None = None
    original: dict[str, Any] | None = None  # the real review's verdict, findings and cost

    def __post_init__(self) -> None:
        if not _KEY.match(self.task_key):
            raise ValueError(f"unexpected task key {self.task_key!r}")
        if not (_SHA.match(self.head_sha) and _SHA.match(self.base_sha)):
            raise ValueError(f"{self.task_key}: commits must be full SHAs")
        if self.role not in ROLES:
            raise ValueError(f"{self.task_key}: unknown role {self.role!r}")


def load_cases(path: Path) -> list[Case]:
    cases = [Case(**c) for c in json.loads(path.read_text())]
    for c in cases:
        if not c.repo_url.startswith("https://github.com/"):
            raise ValueError(f"{c.task_key}: only GitHub repositories")
    return cases


def run_name(case: Case, model: str) -> str:
    """A workspace name that says what ran, and stays a plain directory name."""
    short = model.removeprefix("claude-").split("-")[0]
    return f"cmp-{case.task_key.lower()}-{case.role.split('_')[0]}-{short}"


@dataclass(frozen=True)
class Snapshot:
    repo: Path
    out: Path
    diff: str


def prepare_snapshot(root: Path, name: str, case: Case, token: str | None) -> Snapshot:
    """Clone, fetch the PR's commits (they outlive its branch), check out the reviewed commit and
    diff it against the merge base with the PR's base, as the original reviewers saw it."""
    ws = root / name
    if ws.exists():
        raise FileExistsError(f"{ws} already exists: one run per case and model")
    repo, out = ws / "repo", ws / "out"
    out.mkdir(parents=True)
    git(["clone", "--quiet", case.repo_url, str(repo)], token=token)
    git(["fetch", "--quiet", "origin", f"refs/pull/{case.pr}/head"], cwd=repo, token=token)
    git(["checkout", "--quiet", "--detach", case.head_sha], cwd=repo)
    diff = git(
        ["diff", "--no-color", "--find-renames", f"{case.base_sha}...{case.head_sha}"], cwd=repo
    )
    seal(repo)
    return Snapshot(repo=repo, out=out, diff=diff)


def request_for(case: Case, snap: Snapshot, model: str, budget: Decimal) -> ReviewRequest:
    return ReviewRequest(
        task_key=case.task_key,
        review_id=0,
        role=case.role,
        title=case.title,
        specification=case.specification,
        acceptance_criteria=case.acceptance_criteria,
        allowed_paths=case.allowed_paths,
        diff=snap.diff,
        developer_result=case.developer_result,
        workspace=snap.repo,
        output_dir=snap.out,
        budget_usd=budget,
        model=model,
    )


def result_of(case: Case, model: str, outcome: ReviewOutcome, diff: str) -> dict[str, Any]:
    """What the comparison keeps. Model text is redacted; a credential in it is flagged, never
    stored, and stops the drill (a human decides what happens next)."""
    contract = outcome.contract.model_dump(mode="json") if outcome.contract else None
    text = json.dumps(contract) if contract else ""
    everything = text + (outcome.error or "")
    # Shaped like a credential, or the exact value of one this process holds.
    leaked = bool(credentials_in(everything)) or redact(everything) != everything
    if leaked:
        contract = json.loads(redact(text)) if contract else None
    return {
        "task_key": case.task_key,
        "pr": case.pr,
        "role": case.role,
        "model": model,
        "outcome": outcome.outcome.value,
        "verdict": contract.get("verdict") if contract else None,
        "summary": contract.get("summary") if contract else None,
        "findings": contract.get("findings", []) if contract else [],
        "error": redact(outcome.error) if outcome.error else None,
        "cost_usd": str(outcome.cost_usd) if outcome.cost_usd is not None else None,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "turns": outcome.turns,
        "diff_chars": len(diff),
        "credential_found": leaked,
    }


@dataclass
class Spend:
    """The drill's own cap: a run is started only while the metered total is under it."""

    cap: Decimal
    spent: Decimal = Decimal(0)
    reserved: Decimal = Decimal(0)
    stopped: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, budget: Decimal) -> bool:
        with self.lock:
            if self.stopped or self.spent + self.reserved + budget > self.cap:
                return False
            self.reserved += budget
            return True

    def settle(self, budget: Decimal, cost: Decimal | None) -> None:
        with self.lock:
            self.reserved -= budget
            self.spent += cost if cost is not None else budget  # unknown cost counts in full


def run_all(
    cases: Iterable[Case],
    models: list[str],
    *,
    root: Path,
    results: Path,
    runtime_factory: Callable[[], ReviewRuntime],
    token: str | None,
    budget: Decimal,
    spend: Spend,
    parallel: int = 2,
) -> list[dict[str, Any]]:
    """Run every case on every model, at most `parallel` at once, appending each result."""
    for m in models:
        if not _MODEL.match(m):
            raise ValueError(f"unexpected model {m!r}")
    jobs = [(c, m) for m in models for c in cases]
    done: list[dict[str, Any]] = []
    write = threading.Lock()

    def one(case: Case, model: str) -> None:
        if not spend.take(budget):
            log.warning("skipped %s %s on %s: drill cap", case.task_key, case.role, model)
            return
        cost: Decimal | None = None
        try:
            snap = prepare_snapshot(root, run_name(case, model), case, token)
            outcome = runtime_factory().run_review(request_for(case, snap, model, budget))
            cost = outcome.cost_usd
            row = result_of(case, model, outcome, snap.diff)
        except Exception as e:  # recorded, never retried: the run may have spent money
            row = {"task_key": case.task_key, "pr": case.pr, "role": case.role, "model": model,
                   "outcome": "ERROR", "error": redact(f"{type(e).__name__}: {e}")}  # fmt: skip
        finally:
            spend.settle(budget, cost)
        if row.get("credential_found"):
            spend.stopped = f"credential in {case.task_key} {case.role} on {model}"
        with write:
            done.append(row)
            with results.open("a") as f:
                f.write(json.dumps(row) + "\n")
        log.info("%s %s %s: %s %s $%s", case.task_key, case.role, model,
                 row["outcome"], row.get("verdict"), row.get("cost_usd"))  # fmt: skip

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for f in [pool.submit(one, c, m) for c, m in jobs]:
            f.result()
    return done


def summarize(rows: list[dict[str, Any]], cases: list[Case]) -> dict[str, Any]:
    """Per model: runs, verdicts, findings by severity, cost; and how often each model agreed
    with the original verdict on the same commit and role."""
    originals = {(c.task_key, c.role): (c.original or {}).get("verdict") for c in cases}
    out: dict[str, Any] = {}
    for row in rows:
        m = out.setdefault(row["model"], {"runs": 0, "completed": 0, "verdicts": {},
                                          "findings": {}, "cost_usd": Decimal(0),
                                          "agree": 0, "compared": 0})  # fmt: skip
        m["runs"] += 1
        if row.get("cost_usd") is not None:
            m["cost_usd"] += Decimal(row["cost_usd"])
        if row.get("outcome") != Outcome.COMPLETED.value:
            continue
        m["completed"] += 1
        v = row.get("verdict")
        m["verdicts"][v] = m["verdicts"].get(v, 0) + 1
        for f in row.get("findings") or []:
            s = f.get("severity", "?")
            m["findings"][s] = m["findings"].get(s, 0) + 1
        original = originals.get((row["task_key"], row["role"]))
        if original is not None:
            m["compared"] += 1
            m["agree"] += int(original == v)
    for m in out.values():
        m["cost_usd"] = str(m["cost_usd"])
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from axiom.runtimes.sandbox import SandboxRuntime
    from axiom.worker.__main__ import launcher, model_proxy

    cases_file, results_file, *models = argv
    env = os.environ
    proxy = model_proxy()
    if proxy is None:
        raise SystemExit("no model proxy token: refusing to run")
    root = Path(env["AXIOM_WORKSPACES_DIR"])
    cases = load_cases(Path(cases_file))
    spend = Spend(cap=Decimal(env.get("AXIOM_COMPARE_CAP_USD", "12")))

    def runtime() -> ReviewRuntime:
        return SandboxRuntime(
            network=env.get("AXIOM_SANDBOX_NETWORK", "axiom_sandbox"),
            image=env.get("AXIOM_SANDBOX_IMAGE", "axiom-sandbox:latest"),
            model_proxy=proxy,
            launcher=launcher(),
        )

    rows = run_all(
        cases,
        models,
        root=root,
        results=Path(results_file),
        runtime_factory=runtime,
        token=env.get("AXIOM_GITHUB_TOKEN"),
        budget=Decimal(env.get("AXIOM_COMPARE_RUN_USD", "1.50")),
        spend=spend,
        parallel=int(env.get("AXIOM_COMPARE_PARALLEL", "2")),
    )
    print(json.dumps(summarize(rows, cases), indent=2))
    if spend.stopped:
        print(f"STOPPED: {spend.stopped}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
