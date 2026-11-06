"""What a run cost, when it was killed before it could say (ADR-012, Phase 4 T3).

The SDK reports a run's cost in its final message. A killed run has none, only a transcript of
per-message usage. That usage is exact for input and cache tokens but not for output: mid-run
messages carry a running count (120 tokens where 2,481 were billed), and thinking is billed but
not shown. So output is estimated from the text that is shown, scaled up.

Checked against six real runs: raw usage was 24-37% low. Billed output was 2.07-2.48 times
chars/4, so OUTPUT_SCALE errs slightly high, the safe side for a budget. A small sample: every
such cost is stored with cost_estimated = true and is never presented as a billed amount.
The transcript is written in the sandbox, so it is parsed as untrusted input.
"""

import dataclasses
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom.runtimes.base import RunOutcome
from axiom.runtimes.review import ReviewOutcome

# $ per million tokens: input, output, cache read. Cache writes: input x 1.25 (5m), x 2 (1h).
PRICES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal(4), Decimal(20), Decimal("0.20")),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10), Decimal("0.20")),
    "claude-fable-5-1": (Decimal(10), Decimal(50), Decimal("0.25")),
}
UNKNOWN_MODEL = PRICES["claude-fable-5-1"]  # the dearest: an estimate shouldn't be flattering
OUTPUT_SCALE = Decimal("2.5")
CHARS_PER_TOKEN = 4
MAX_ESTIMATE = Decimal(1000)  # a corrupt transcript must not overflow the column
MILLION = Decimal(1_000_000)
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class CostEstimate:
    usd: Decimal
    input_tokens: int
    output_tokens: int


def _count(mapping: Any, key: str) -> int:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def _shown_chars(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, dict):
            for key in ("text", "thinking"):
                if isinstance(block.get(key), str):
                    total += len(block[key])
            if "input" in block:
                total += len(json.dumps(block["input"], default=str))
    return total


def _message_cost(model: Any, usage: Any, chars: int) -> tuple[Decimal, int, int]:
    input_price, output_price, read_price = PRICES.get(model, UNKNOWN_MODEL)
    fresh, written, read = (_count(usage, f) for f in TOKEN_FIELDS)
    detail = usage.get("cache_creation") if isinstance(usage, dict) else None
    hour = min(_count(detail, "ephemeral_1h_input_tokens"), written)
    scaled = int(Decimal(chars) / CHARS_PER_TOKEN * OUTPUT_SCALE)
    output = max(_count(usage, "output_tokens"), scaled)
    usd = (
        fresh * input_price
        + (written - hour) * input_price * Decimal("1.25")
        + hour * input_price * 2
        + read * read_price
        + output * output_price
    ) / MILLION
    return usd, fresh + written + read, output


def estimate_from_transcript(path: Path) -> CostEstimate | None:
    """None if the file is missing or shows no spending. Damaged lines are skipped: a killed
    process can leave half a line."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    messages: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        data = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(data, dict):
            continue
        if entry.get("type") == "ResultMessage" and isinstance(data.get("total_cost_usd"), float):
            # The run finished and only the worker died: this figure is the SDK's own.
            usage = data.get("usage")
            return CostEstimate(
                min(Decimal(str(max(data["total_cost_usd"], 0.0))), MAX_ESTIMATE),
                sum(_count(usage, f) for f in TOKEN_FIELDS),
                _count(usage, "output_tokens"),
            )
        if entry.get("type") == "AssistantMessage":
            # One API message spans several transcript lines, one per content block.
            message = messages.setdefault(str(data.get("message_id")), {"chars": 0})
            message["chars"] += _shown_chars(data.get("content"))
            message["usage"], message["model"] = data.get("usage"), data.get("model")
    if not messages:
        return None
    usd, tokens_in, tokens_out = Decimal(0), 0, 0
    for m in messages.values():
        cost, fresh, output = _message_cost(m["model"], m["usage"], m["chars"])
        usd, tokens_in, tokens_out = usd + cost, tokens_in + fresh, tokens_out + output
    return CostEstimate(min(usd, MAX_ESTIMATE).quantize(Decimal("0.0001")), tokens_in, tokens_out)


def with_estimate[Reported: (RunOutcome, ReviewOutcome)](
    outcome: Reported, output_dir: Path
) -> Reported:
    """Fill in an estimate when a run outcome has no cost; a reported cost is never replaced."""
    if outcome.cost_usd is not None:
        return outcome
    found = estimate_from_transcript(output_dir / "transcript.jsonl")
    if found is None:
        return outcome
    return dataclasses.replace(
        outcome,
        cost_usd=found.usd,
        input_tokens=found.input_tokens,
        output_tokens=found.output_tokens,
        cost_estimated=True,
    )
