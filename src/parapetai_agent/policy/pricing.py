"""Config-driven model pricing for real-time cumulative cost tracking
(policy/cost_tracker.py).

Deliberately the SAME table shape and PARAPETAI_MODEL_PRICING env var as
parapetai_control.pricing (the control plane's retrospective cost-panel
rollup, computed after the fact from exported spans) -- an operator who
sets pricing overrides once should not need to set it twice for the two
numbers (a live budget decision here, a dashboard rollup there) to agree.
Not imported directly from parapetai_control: this package has no runtime
dependency on the control plane (CLAUDE.md -- the control plane is never on
the decision path), so this is its own small copy, same convention already
used for policy_index.py's Cedar-file rendering in that repo. If the two
drift, it shows up as the SDK's live enforcement disagreeing with the
control plane's own cost panel for the same traffic -- not a security bug
(pricing is an estimate, not a Cedar-engine-identity concern), but still
worth noticing; keep this table's model list in sync with
parapetai_control/pricing.py's `_DEFAULTS` by hand.

Returns integer MICRO-USD (1_000_000 == $1), never a float -- see
cost_tracker.py's module docstring for why: cedarpy has no native float
context type, and a bare Python float silently stringifies before reaching
Cedar (policy/engine.py's _cedar_leaf), breaking a numeric `when` clause.

Defaults are public list prices at authoring time and are a convenience,
not a source of truth: an unknown model returns None (unpriced) rather
than a guessed rate, so a budget policy never silently treats an unpriced
call as free.
"""

from __future__ import annotations

import json
import os
from typing import Any

# $ per 1,000,000 tokens, (input, output). Public list prices, best-effort;
# override per-deployment via PARAPETAI_MODEL_PRICING. Matched by exact id
# first, then longest-prefix (so "gpt-4o-2024-08-06" resolves to "gpt-4o").
# Kept in sync BY HAND with parapetai_control/pricing.py's own _DEFAULTS --
# see this module's own docstring for why that's a deliberate small copy,
# not an import.
_DEFAULTS: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "gpt-4.1-nano": {"input": 0.1, "output": 0.4},
    "o4-mini": {"input": 1.1, "output": 4.4},
    "o3": {"input": 2.0, "output": 8.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.3, "output": 2.5},
}


def _table() -> dict[str, dict[str, float]]:
    """The active price table: defaults, with any PARAPETAI_MODEL_PRICING
    entries merged over them. A malformed override is ignored wholesale
    (fall back to defaults) rather than half-applied."""
    override = os.environ.get("PARAPETAI_MODEL_PRICING", "").strip()
    if not override:
        return dict(_DEFAULTS)
    try:
        parsed: Any = json.loads(override)
        if not isinstance(parsed, dict):
            return dict(_DEFAULTS)
    except (json.JSONDecodeError, TypeError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    for model, rate in parsed.items():
        if isinstance(rate, dict) and "input" in rate and "output" in rate:
            try:
                merged[model] = {"input": float(rate["input"]), "output": float(rate["output"])}
            except (TypeError, ValueError):
                continue
    return merged


def price_for(model: str | None) -> dict[str, float] | None:
    """Rate for a model id, or None if unpriced. Exact match first, then
    the longest matching prefix so dated/tagged variants resolve to their
    base."""
    if not model:
        return None
    table = _table()
    if model in table:
        return table[model]
    for name in sorted(table, key=len, reverse=True):
        if model.startswith(name):
            return table[name]
    return None


def estimate_cost_usd_micros(
    model: str | None, prompt_tokens: int, completion_tokens: int
) -> int | None:
    """Integer micro-USD for one call, or None if the model is unpriced
    (caller must treat that as "unknown", never as $0 -- an unpriced
    expensive model silently bypassing a budget policy is worse than a
    denied call the operator has to price)."""
    rate = price_for(model)
    if rate is None:
        return None
    cost_usd = (prompt_tokens / 1_000_000) * rate["input"] + (
        completion_tokens / 1_000_000
    ) * rate["output"]
    return round(cost_usd * 1_000_000)
