"""Phase 4 T3: what a killed run cost, from its transcript. The numbers come from real runs."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom.runtimes.base import Outcome, RunOutcome
from axiom.runtimes.estimate import (
    MAX_ESTIMATE,
    PRICES,
    estimate_from_transcript,
    with_estimate,
)
from axiom.runtimes.review import ReviewOutcome


def _assistant(
    message_id: str = "m1", model: str = "claude-opus-5-5", text: str = "", **usage: Any
) -> dict[str, Any]:
    return {
        "type": "AssistantMessage",
        "data": {
            "message_id": message_id,
            "model": model,
            "usage": usage,
            "content": [{"text": text}],
        },
    }


def _write(path: Path, *entries: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_prices_reproduce_a_real_billed_run(tmp_path: Path) -> None:
    """SOC-002-r4 on Fable 5.1: these tokens were billed at exactly $0.21327525."""
    found = estimate_from_transcript(
        _write(
            tmp_path / "t.jsonl",
            _assistant(
                model="claude-fable-5-1",
                input_tokens=34,
                cache_creation_input_tokens=12492,
                cache_read_input_tokens=19541,
                output_tokens=1038,
            ),
        )
    )
    assert found is not None and found.usd == Decimal("0.2133")
    assert found.input_tokens == 34 + 12492 + 19541 and found.output_tokens == 1038


def test_killed_run_estimate_is_close_to_what_a_real_run_cost(tmp_path: Path) -> None:
    """SOC-001 really cost $0.1459. Mid-run usage showed 120 output tokens, not the 2,481 billed;
    usage alone would say $0.0987. The output is estimated from the text shown instead."""
    found = estimate_from_transcript(
        _write(
            tmp_path / "t.jsonl",
            _assistant(
                text="x" * 4008,  # 1,002 tokens' worth of visible output
                input_tokens=16,
                cache_creation_input_tokens=15682,
                cache_read_input_tokens=89212,
                output_tokens=120,
            ),
        )
    )
    assert found is not None
    assert abs(found.usd - Decimal("0.1459")) / Decimal("0.1459") < Decimal("0.05")


def test_one_message_over_several_lines_is_counted_once(tmp_path: Path) -> None:
    line = {"input_tokens": 1_000_000, "output_tokens": 0}
    one = estimate_from_transcript(_write(tmp_path / "a", _assistant(**line)))
    two = estimate_from_transcript(_write(tmp_path / "b", _assistant(**line), _assistant(**line)))
    assert one is not None and two is not None and one.usd == two.usd == Decimal(4)


def test_cache_writes_are_priced_by_their_lifetime(tmp_path: Path) -> None:
    found = estimate_from_transcript(
        _write(
            tmp_path / "t",
            _assistant(
                cache_creation_input_tokens=2_000_000,
                cache_creation={"ephemeral_1h_input_tokens": 1_000_000},
            ),
        )
    )
    assert found is not None and found.usd == Decimal(5 + 8)  # 1M x $5 + 1M x $8


def test_unknown_model_is_priced_as_the_dearest(tmp_path: Path) -> None:
    found = estimate_from_transcript(
        _write(tmp_path / "t", _assistant(model="claude-new", input_tokens=1_000_000))
    )
    assert found is not None and found.usd == PRICES["claude-fable-5-1"][0]


def test_a_finished_run_uses_the_sdks_own_figure(tmp_path: Path) -> None:
    """The run finished and only the worker died."""
    result = {
        "type": "ResultMessage",
        "data": {"total_cost_usd": 0.1722, "usage": {"input_tokens": 9, "output_tokens": 7}},
    }
    found = estimate_from_transcript(_write(tmp_path / "t", _assistant(input_tokens=5), result))
    assert found is not None
    assert (found.usd, found.input_tokens, found.output_tokens) == (Decimal("0.1722"), 9, 7)


def test_half_written_and_foreign_lines_are_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path / "t", _assistant(input_tokens=1_000_000), {"scenario": "x"}, {"a": 1})
    with path.open("a") as f:
        f.write('[1, 2]\n"text"\n{"type": "AssistantMessage", "data": {"mess')
    found = estimate_from_transcript(path)
    assert found is not None and found.usd == Decimal(4)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -5, "output_tokens": "many"},
        {"input_tokens": None},
        {"cache_creation": "x", "input_tokens": 1.5},
    ],
)
def test_nonsense_usage_counts_as_nothing(tmp_path: Path, usage: dict[str, Any]) -> None:
    found = estimate_from_transcript(_write(tmp_path / "t", _assistant(**usage)))
    assert found is not None and found.usd == 0


def test_absurd_usage_cannot_overflow_the_column(tmp_path: Path) -> None:
    found = estimate_from_transcript(_write(tmp_path / "t", _assistant(input_tokens=10**15)))
    assert found is not None and found.usd == MAX_ESTIMATE.quantize(Decimal("0.0001"))


def test_nothing_to_estimate_from(tmp_path: Path) -> None:
    assert estimate_from_transcript(tmp_path / "missing") is None
    assert estimate_from_transcript(_write(tmp_path / "t", {"scenario": "fake"})) is None


@pytest.mark.parametrize("outcome_type", [RunOutcome, ReviewOutcome])
def test_a_missing_cost_is_estimated_and_marked(tmp_path: Path, outcome_type: Any) -> None:
    _write(tmp_path / "transcript.jsonl", _assistant(input_tokens=1_000_000))
    filled = with_estimate(outcome_type(Outcome.FAILED, error="timed out"), tmp_path)
    assert filled.cost_usd == Decimal(4) and filled.cost_estimated is True
    assert filled.cost_payload()["estimated"] is True and filled.error == "timed out"


def test_a_reported_cost_is_never_replaced(tmp_path: Path) -> None:
    _write(tmp_path / "transcript.jsonl", _assistant(input_tokens=1_000_000))
    reported = RunOutcome(Outcome.FAILED, cost_usd=Decimal("0.01"))
    assert with_estimate(reported, tmp_path) is reported
    assert reported.cost_payload()["estimated"] is False


def test_without_a_transcript_the_outcome_is_unchanged(tmp_path: Path) -> None:
    outcome = RunOutcome(Outcome.CANCELLED)
    assert with_estimate(outcome, tmp_path) is outcome


def test_fable_5_2_and_sonnet_5_5_are_priced_as_published(tmp_path: Path) -> None:
    """Fable 5.2: $8 / $40, cache reads $0.20. Sonnet 5.5: $2 / $10, cache reads $0.10."""
    fable = estimate_from_transcript(
        _write(
            tmp_path / "f",
            _assistant(
                model="claude-fable-5-2", input_tokens=1_000_000, cache_read_input_tokens=1_000_000
            ),
        )
    )
    sonnet = estimate_from_transcript(
        _write(
            tmp_path / "s",
            _assistant(
                model="claude-sonnet-5-5", input_tokens=1_000_000, cache_read_input_tokens=1_000_000
            ),
        )
    )
    assert fable is not None and fable.usd == Decimal("8.20")
    assert sonnet is not None and sonnet.usd == Decimal("2.10")
