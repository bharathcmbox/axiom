"""Phase 4 T3: a killed worker's runs are costed when it comes back."""

import json
from pathlib import Path
from typing import Any

import pytest

from axiom.worker.api_client import ApiError
from axiom.worker.recover_costs import recover_costs

USAGE = {"input_tokens": 1_000_000}


class FakeApi:
    def __init__(self, executions: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> None:
        self._executions, self._reviews = executions, reviews
        self.recorded: list[tuple[str, int, dict[str, Any]]] = []

    def executions(self, key: str) -> list[dict[str, Any]]:
        return self._executions

    def task_reviews(self, key: str) -> list[dict[str, Any]]:
        return self._reviews

    def record_execution_cost(self, run_id: int, cost: dict[str, Any]) -> None:
        self.recorded.append(("e", run_id, cost))

    def record_review_cost(self, run_id: int, cost: dict[str, Any]) -> None:
        self.recorded.append(("r", run_id, cost))


def _run_dir(root: Path, name: str, *, transcript: bool = True) -> None:
    out = root / name / "out"
    out.mkdir(parents=True)
    if transcript:
        line = {"type": "AssistantMessage", "data": {"message_id": "m", "usage": USAGE}}
        (out / "transcript.jsonl").write_text(json.dumps(line) + "\n")


def _recover(api: FakeApi, root: Path, kinds: str) -> int:
    return recover_costs(api, root, kinds=kinds)  # type: ignore[arg-type]


def test_a_killed_developer_run_is_costed_as_an_estimate(tmp_path: Path) -> None:
    _run_dir(tmp_path, "SOC-1-e7")
    api = FakeApi([{"id": 7, "status": "TIMED_OUT", "cost_usd": None}], [])
    assert _recover(api, tmp_path, "e") == 1
    kind, run_id, cost = api.recorded[0]
    assert (kind, run_id, cost["estimated"]) == ("e", 7, True)


def test_a_killed_review_run_is_costed_too(tmp_path: Path) -> None:
    _run_dir(tmp_path, "SOC-1-r3")
    api = FakeApi([], [{"id": 3, "status": "PENDING", "cost_usd": None}])
    assert _recover(api, tmp_path, "r") == 1
    assert api.recorded[0][:2] == ("r", 3)


@pytest.mark.parametrize(
    "row",
    [
        {"id": 7, "status": "RUNNING", "cost_usd": None},  # someone is still working
        {"id": 7, "status": "STARTING", "cost_usd": None},
        {"id": 7, "status": "COMPLETED", "cost_usd": "0.15"},  # it reported: a kept workspace
        {"id": 8, "status": "TIMED_OUT", "cost_usd": None},  # not this directory's run
    ],
)
def test_runs_that_need_nothing_are_left_alone(tmp_path: Path, row: dict[str, Any]) -> None:
    _run_dir(tmp_path, "SOC-1-e7")
    api = FakeApi([row], [])
    assert _recover(api, tmp_path, "e") == 0 and api.recorded == []


@pytest.mark.parametrize(
    "row",
    [
        {"id": 3, "status": "CLAIMED", "cost_usd": None},  # another reviewer's live run
        {"id": 3, "status": "PASS", "cost_usd": "0.25"},
    ],
)
def test_reviews_that_need_nothing_are_left_alone(tmp_path: Path, row: dict[str, Any]) -> None:
    _run_dir(tmp_path, "SOC-1-r3")
    api = FakeApi([], [row])
    assert _recover(api, tmp_path, "r") == 0 and api.recorded == []


def test_each_worker_looks_only_at_its_own_kind_of_run(tmp_path: Path) -> None:
    _run_dir(tmp_path, "SOC-1-e7")
    _run_dir(tmp_path, "SOC-1-r3")
    api = FakeApi(
        [{"id": 7, "status": "FAILED", "cost_usd": None}],
        [{"id": 3, "status": "ERROR", "cost_usd": None}],
    )
    assert _recover(api, tmp_path, "e") == 1
    assert [r[0] for r in api.recorded] == ["e"]


def test_other_directories_and_missing_transcripts_are_ignored(tmp_path: Path) -> None:
    _run_dir(tmp_path, "demo")
    _run_dir(tmp_path, "SOC-1-e7", transcript=False)
    api = FakeApi([{"id": 7, "status": "FAILED", "cost_usd": None}], [])
    assert _recover(api, tmp_path, "e") == 0
    assert _recover(api, tmp_path / "nope", "e") == 0


def test_someone_elses_run_does_not_stop_the_sweep(tmp_path: Path) -> None:
    _run_dir(tmp_path, "SOC-1-e7")
    _run_dir(tmp_path, "SOC-1-e8")

    class Refusing(FakeApi):
        def record_execution_cost(self, run_id: int, cost: dict[str, Any]) -> None:
            if run_id == 7:
                raise ApiError(403, {"error": "not_owner", "message": "not yours"})
            super().record_execution_cost(run_id, cost)

    api = Refusing(
        [
            {"id": 7, "status": "FAILED", "cost_usd": None},
            {"id": 8, "status": "FAILED", "cost_usd": None},
        ],
        [],
    )
    assert _recover(api, tmp_path, "e") == 1 and api.recorded[0][1] == 8
