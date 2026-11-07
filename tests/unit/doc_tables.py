"""Parse the transition tables in docs/state-machines.md so tests can compare them with code."""

import re
from dataclasses import dataclass
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs" / "state-machines.md"

TASK_STATES = [
    "PENDING", "READY", "ASSIGNED", "IN_PROGRESS", "WAITING_FOR_HUMAN",
    "IN_REVIEW", "AWAITING_MERGE", "DONE", "FAILED", "CANCELLED",
]  # fmt: skip
NON_TERMINAL = [s for s in TASK_STATES if s not in ("DONE", "CANCELLED")]


@dataclass(frozen=True)
class DocTaskRow:
    commands: tuple[str, ...]
    event: str
    sources: frozenset[str]
    targets: tuple[str, ...]
    actor_text: str


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _states(cell: str) -> frozenset[str]:
    if cell == "any non-terminal":
        return frozenset(NON_TERMINAL)
    if cell == "—":
        return frozenset()
    return frozenset(s.strip() for s in cell.split(","))


def task_rows() -> list[DocTaskRow]:
    rows = []
    for line in DOC.read_text().splitlines():
        if not re.match(r"^\| \d+ \|", line):
            continue
        _, cmd_event, src, dst, actor, *_ = _cells(line)
        cmds_part, event = (p.strip() for p in cmd_event.split("→"))
        commands = tuple(re.findall(r"`(\w+)`", cmds_part))
        targets = tuple(t.strip() for t in dst.split("/"))
        rows.append(DocTaskRow(commands, event, _states(src), targets, actor))
    return rows


def execution_rows() -> list[tuple[str, frozenset[str], str, tuple[str, ...]]]:
    """(transition name, sources, target, causing task commands)."""
    rows = []
    in_table = False
    for line in DOC.read_text().splitlines():
        if line.startswith("| Execution transition"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if line.startswith("|---"):
                continue
            name, src, dst, cmds = _cells(line)
            rows.append((name, _states(src), dst, tuple(re.findall(r"`(\w+)`", cmds))))
    return rows


def review_rows() -> list[tuple[str, frozenset[str], tuple[str, ...], str, str]]:
    """(command, sources, targets, actor, event) from the review table."""
    rows, in_table = [], False
    for line in DOC.read_text().splitlines():
        if line.startswith("| Review command"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if line.startswith("|---"):
                continue
            cmd, src, dst, actor, event = _cells(line)
            sources = frozenset(x.strip() for x in src.split(","))
            targets = tuple(t.strip() for t in dst.split("/"))
            rows.append((cmd.strip("`"), sources, targets, actor, event))
    return rows


def plan_rows() -> list[tuple[str, frozenset[str], tuple[str, ...], str, str]]:
    """(command, sources, targets, actor, event) from the plan table."""
    rows, in_table = [], False
    for line in DOC.read_text().splitlines():
        if line.startswith("| Plan command"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if line.startswith("|---"):
                continue
            cmd, src, dst, actor, event = _cells(line)
            sources = frozenset(x.strip() for x in src.split(","))
            targets = tuple(t.strip() for t in dst.split("/"))
            rows.append((cmd.strip("`"), sources, targets, actor, event))
    return rows
