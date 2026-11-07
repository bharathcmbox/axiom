"""What makes a plan acceptable (ADR-011). Pure functions, so every rule is tested alone.

A plan is a model's output, so it's checked before a human sees it. A failure counts as a failed
attempt. These rules are about the plan's shape: whether the plan is any good is the human's call.
"""

import json
from decimal import Decimal

from axiom.contracts import PlanContract, PlanTask
from axiom.paths import is_secret, normalize
from axiom.worker.verify import credentials_in

MAX_TASKS = 8
WILDCARD_CHARS = "*?["
# Who changes these changes what "passing" means, so no task may hold them: CI defines the gate, and
# .git isn't part of the work.
PROTECTED_TOP_LEVEL = (".github", ".git")


def _depends_on_all(tasks: list[PlanTask]) -> dict[str, set[str]]:
    """Everything each task waits for, directly or not. Assumes no cycles."""
    direct = {t.ref: set(t.depends_on) for t in tasks}
    resolved: dict[str, set[str]] = {}

    def resolve(ref: str) -> set[str]:
        if ref not in resolved:
            resolved[ref] = set()
            for dep in direct.get(ref, ()):
                resolved[ref] |= {dep} | resolve(dep)
        return resolved[ref]

    for ref in direct:
        resolve(ref)
    return resolved


def has_cycle(tasks: list[PlanTask]) -> bool:
    """Kahn's algorithm: repeatedly remove tasks nothing unfinished points at."""
    known = {t.ref for t in tasks}
    waiting = {t.ref: {d for d in t.depends_on if d in known} for t in tasks}
    while waiting:
        free = [ref for ref, deps in waiting.items() if not deps]
        if not free:
            return True
        for ref in free:
            del waiting[ref]
        for deps in waiting.values():
            deps.difference_update(free)
    return False


def _stem(pattern: str) -> tuple[list[str], bool]:
    """The fixed start of a path pattern, and whether it can match more than exactly that."""
    stem: list[str] = []
    parts = pattern.split("/")
    for part in parts:
        if not part:  # a trailing slash: a whole directory
            return stem, True
        if any(c in part for c in WILDCARD_CHARS):
            return stem, True
        stem.append(part)
    return stem, False


def may_overlap(a: str, b: str) -> bool:
    """Could these two patterns match the same file? When unsure, yes: a false alarm costs a
    planning retry, and a miss costs a merge conflict."""
    stem_a, open_a = _stem(a)
    stem_b, open_b = _stem(b)
    shared = min(len(stem_a), len(stem_b))
    if stem_a[:shared] != stem_b[:shared]:
        return False
    if len(stem_a) == len(stem_b):
        return True
    return open_a if len(stem_a) < len(stem_b) else open_b


def _path_problem(path: str) -> str | None:
    if normalize(path) is None:
        return "must be inside the repository"
    if ".." in path.split("/"):
        return "must not contain .."
    if is_secret(path):
        return "looks like a secret"
    stem, _ = _stem(path)
    if not stem:
        return "is the whole repository"
    if stem[0].lower() in PROTECTED_TOP_LEVEL:
        return f"{stem[0]} may not be changed by a task"
    return None


def within_test_paths(path: str, test_paths: list[str]) -> bool:
    """Is everything this pattern can match inside one of the project's test paths?"""
    stem, is_open = _stem(path)
    for allowed in test_paths:
        allowed_stem, allowed_open = _stem(allowed)
        if allowed_open:
            if stem[: len(allowed_stem)] == allowed_stem and "**" not in allowed.split("/")[:-1]:
                return True
        elif not is_open and stem == allowed_stem:
            return True
    return False


def validate_plan(plan: PlanContract, *, budget_usd: Decimal, test_paths: list[str]) -> list[str]:
    """Every problem with a plan, in words the next planning attempt can act on."""
    problems: list[str] = []
    tasks = plan.tasks
    if not tasks:
        return ["a plan needs at least one task"]
    if len(tasks) > MAX_TASKS:
        problems.append(f"a plan may have at most {MAX_TASKS} tasks, not {len(tasks)}")

    refs = [t.ref for t in tasks]
    for ref in sorted({r for r in refs if refs.count(r) > 1}):
        problems.append(f"task ref {ref} is used more than once")
    known = set(refs)
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.ref:
                problems.append(f"{task.ref} depends on itself")
            elif dep not in known:
                problems.append(f"{task.ref} depends on {dep}, which isn't in the plan")
    cyclic = has_cycle(tasks)
    if cyclic:
        problems.append("the dependencies form a cycle")

    for task in tasks:
        if not task.allowed_paths:
            problems.append(f"{task.ref} has no allowed paths")
        for path in task.allowed_paths:
            problem = _path_problem(path)
            if problem:
                problems.append(f"{task.ref}: path {path} {problem}")
            elif task.role == "qa" and not within_test_paths(path, test_paths):
                problems.append(f"{task.ref} is a QA task, and {path} is outside the test paths")

    if not cyclic and len(refs) == len(known):
        waits_for = _depends_on_all(tasks)
        for i, a in enumerate(tasks):
            for b in tasks[i + 1 :]:
                if a.ref in waits_for[b.ref] or b.ref in waits_for[a.ref]:
                    continue  # one finishes first, so they never run at the same time
                clash = [
                    f"{p} and {q}"
                    for p in a.allowed_paths
                    for q in b.allowed_paths
                    if may_overlap(p, q)
                ]
                if clash:
                    problems.append(
                        f"{a.ref} and {b.ref} can run at the same time and may change the same "
                        f"files ({clash[0]}): make one depend on the other, or split the paths"
                    )

    total = sum((Decimal(str(t.budget_usd)) for t in tasks), Decimal(0))
    if total > budget_usd:
        problems.append(f"the tasks' budgets total ${total}, over the plan's ${budget_usd}")

    leaked = credentials_in(json.dumps(plan.model_dump(mode="json")))
    if leaked:
        problems.append(f"the plan's text looks like it holds a {', '.join(leaked)}")
    return problems
