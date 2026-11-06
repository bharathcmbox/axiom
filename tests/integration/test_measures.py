"""Phase 4 T1: metrics from real rows and events, and recording what only a human knows."""

from decimal import Decimal

import pytest

from axiom.domain.task_machine import TaskCommand, TaskState
from axiom.errors import Forbidden, InvalidInput, NotFound
from axiom.orchestrator.measures import collect, record_baseline, record_defect, set_tags
from axiom.orchestrator.reviews import claim_review, submit_review
from axiom.state.db import Conn
from tests.integration.helpers import DEV1, DEV2, HUMAN, do, new_task
from tests.integration.test_reviews import FAIL, PASS, REV, SEC

C, S = TaskCommand, TaskState


def _deliver_with_one_rework(db: Conn) -> str:
    """The shape of SOC-002: a failed review, rework, two passes, a merge."""
    key = new_task(db).key
    do(db, key, C.CLAIM)
    do(db, key, C.START)
    do(db, key, C.SUBMIT, payload={"summary": "r1", "commit": "a", "cost": {"usd": "0.00"}})
    code = claim_review(db, REV)
    assert code is not None
    submit_review(db, REV, code.review_id, FAIL, {"cost": {"usd": "0.26"}})
    do(db, key, C.CLAIM, actor=DEV2)
    do(db, key, C.START, actor=DEV2)
    do(
        db,
        key,
        C.SUBMIT,
        actor=DEV2,
        payload={"summary": "r2", "commit": "b", "cost": {"usd": "0.17"}},
    )
    for actor, usd in ((REV, "0.25"), (SEC, "0.21")):
        claimed = claim_review(db, actor)
        assert claimed is not None
        submit_review(db, actor, claimed.review_id, PASS, {"cost": {"usd": usd}})
    do(db, key, C.MERGE)
    return key


def test_soc_002_shaped_history_reconciles(org: Conn) -> None:
    key = _deliver_with_one_rework(org)
    [m], summary, _ = collect(org)
    assert (m.key, m.merged, m.first_pass, m.review_rounds) == (key, True, False, 1)
    assert m.developer_cost_usd == Decimal("0.17") and m.review_cost_usd == Decimal("0.72")
    assert m.human_interventions == 0
    assert summary.cost_per_merged_usd == Decimal("0.8900")
    assert summary.verdicts == {"FAIL": 1, "PASS": 2}


def test_tag_demo_excludes_from_metrics_by_default(org: Conn) -> None:
    key = new_task(org).key
    other = new_task(org).key
    assert set_tags(org, HUMAN, key, ["demo", "demo"]) == ["demo"]
    assert [m.key for m in collect(org)[0]] == [other]
    assert {m.key for m in collect(org, include_demo=True)[0]} == {key, other}


def test_filter_by_project(org: Conn) -> None:
    new_task(org)
    assert len(collect(org, project_key="AX")[0]) == 1
    assert collect(org, project_key="NOPE")[0] == []


def test_defects_count_on_merged_work(org: Conn) -> None:
    key = _deliver_with_one_rework(org)
    record_defect(org, HUMAN, key, "high incidents still skipped on restart", "high")
    [m], summary, _ = collect(org)
    assert m.defects == 1 and summary.defects_after_merge == 1


def test_baselines_are_recorded_for_comparison(org: Conn) -> None:
    record_baseline(org, HUMAN, project_key="AX", title="similar task by hand", minutes=45,
                    cost_usd=Decimal("0.80"), notes="one session")  # fmt: skip
    _, _, baselines = collect(org, project_key="AX")
    assert baselines[0]["payload"]["minutes"] == 45 and baselines[0]["actor"] == "human:tester"


def test_only_humans_record_and_input_is_checked(org: Conn) -> None:
    key = new_task(org).key
    with pytest.raises(Forbidden):
        set_tags(org, DEV1, key, ["x"])
    with pytest.raises(Forbidden):
        record_defect(org, DEV1, key, "d", "low")
    with pytest.raises(Forbidden):
        record_baseline(org, DEV1, project_key="AX", title="t", minutes=1, cost_usd=Decimal(0))
    with pytest.raises(InvalidInput):
        record_defect(org, HUMAN, key, "d", "catastrophic")
    with pytest.raises(InvalidInput):
        record_baseline(org, HUMAN, project_key="AX", title="t", minutes=-1, cost_usd=Decimal(0))
    with pytest.raises(NotFound):
        record_defect(org, HUMAN, "AX-999", "d", "low")
