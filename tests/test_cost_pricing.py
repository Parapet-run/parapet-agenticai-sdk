"""parapetai_agent.policy.pricing -- same table/env-var shape as
parapetai_control.pricing, but returns integer micro-USD (see
cost_tracker.py's module docstring for why: cedarpy has no native float
context type)."""

from __future__ import annotations

import json

from parapetai_agent.policy.pricing import estimate_cost_usd_micros, price_for


def test_known_model_prices_by_exact_match() -> None:
    rate = price_for("gpt-4o-mini")
    assert rate == {"input": 0.15, "output": 0.6}


def test_dated_variant_resolves_via_longest_prefix() -> None:
    rate = price_for("gpt-4o-2024-08-06")
    assert rate == price_for("gpt-4o")


def test_unknown_model_is_unpriced() -> None:
    assert price_for("some-model-nobody-has-heard-of") is None
    assert price_for(None) is None


def test_estimate_returns_integer_micro_usd() -> None:
    # gpt-4o-mini: $0.15/1M input, $0.60/1M output.
    # 1,000,000 input tokens + 1,000,000 output tokens -> $0.15 + $0.60 = $0.75
    # -> 750,000 micro-USD.
    cost = estimate_cost_usd_micros("gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == 750_000
    assert isinstance(cost, int)


def test_small_call_does_not_round_to_zero_unlike_cents_would() -> None:
    # A single ~500-token call against gpt-4o-mini is a small fraction of a
    # cent -- the whole point of micro-USD over cents (see ADR 0010) is that
    # this is still a meaningful nonzero integer, not $0.00.
    cost = estimate_cost_usd_micros("gpt-4o-mini", 500, 0)
    assert cost is not None
    assert cost > 0
    assert cost < 1_000  # well under a tenth of a cent (1_000 micros = $0.001)


def test_unpriced_model_returns_none_not_zero() -> None:
    assert estimate_cost_usd_micros("totally-unknown-model", 1_000, 1_000) is None


def test_env_override_replaces_a_known_rate() -> None:
    import os

    os.environ["PARAPETAI_MODEL_PRICING"] = json.dumps({"gpt-4o": {"input": 1.0, "output": 2.0}})
    try:
        assert price_for("gpt-4o") == {"input": 1.0, "output": 2.0}
        # Unrelated models are untouched by a partial override.
        assert price_for("gpt-4o-mini") == {"input": 0.15, "output": 0.6}
    finally:
        del os.environ["PARAPETAI_MODEL_PRICING"]


def test_malformed_env_override_falls_back_to_defaults_wholesale() -> None:
    import os

    os.environ["PARAPETAI_MODEL_PRICING"] = "not json"
    try:
        assert price_for("gpt-4o") == {"input": 2.5, "output": 10.0}
    finally:
        del os.environ["PARAPETAI_MODEL_PRICING"]
