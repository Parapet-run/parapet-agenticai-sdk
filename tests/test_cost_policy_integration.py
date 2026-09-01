"""Cross-package correctness test for docs/adr/0010: proves a REAL
cedarpy-backed PolicyEngine actually denies once CostTracker reports a
cumulative total over a hand-authored Cedar threshold -- not just that
CostTracker's own bookkeeping is right (test_cost_tracker.py) or that
pricing.py's arithmetic is right (test_cost_pricing.py) in isolation, but
that the two actually compose correctly through cedarpy's real numeric
`when` clause evaluation.

This is exactly the failure mode ADR 0010 and policy/engine.py's own
_cedar_leaf docstring warn about: a bare Python float silently stringifies
before reaching Cedar, which would make a `context.trace_cumulative_cost_usd_micros
> N` clause either always-false or an evaluation error, never a real
comparison. Only a live cedarpy round trip catches that -- a mock or a unit
test against CostTracker alone would not."""

from __future__ import annotations

from pathlib import Path

from parapetai_agent.policy.cost_tracker import CostTracker
from parapetai_agent.policy.engine import PolicyEngine


def _engine(tmp_path: Path, forbid_when: str) -> PolicyEngine:
    (tmp_path / "00-base.cedar").write_text(
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
    )
    (tmp_path / "10-budget.cedar").write_text(
        f'forbid(principal, action == Action::"model_call", resource)\n'
        f"when {{ {forbid_when} }};\n"
    )
    return PolicyEngine(tmp_path)


def _decide(
    engine: PolicyEngine, tracker: CostTracker, trace_id: str, scope_id: str | None
) -> bool:
    context = tracker.context_for(trace_id=trace_id, scope_id=scope_id)
    decision = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context=context
    )
    return decision.allowed


def test_trace_token_budget_denies_once_crossed(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "context.trace_cumulative_tokens >= 1000")
    tracker = CostTracker()
    trace_id = "trace-a"

    # First call: nothing spent yet in this trace -- allowed.
    assert _decide(engine, tracker, trace_id, None) is True

    # That call's usage becomes known -- record it.
    tracker.record(trace_id=trace_id, scope_id=None, tokens=1200, cost_usd_micros=0)

    # Next call in the SAME trace: cumulative tokens (1200) now over budget.
    assert _decide(engine, tracker, trace_id, None) is False


def test_trace_token_budget_is_per_trace_not_global(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "context.trace_cumulative_tokens >= 1000")
    tracker = CostTracker()
    tracker.record(trace_id="trace-a", scope_id=None, tokens=5000, cost_usd_micros=0)

    # trace-a is well over budget...
    assert _decide(engine, tracker, "trace-a", None) is False
    # ...but a DIFFERENT trace has spent nothing and is unaffected.
    assert _decide(engine, tracker, "trace-b", None) is True


def test_dollar_budget_uses_integer_micros_not_a_float(tmp_path: Path) -> None:
    # This is the specific failure mode ADR 0010 exists to avoid: if cost
    # were a bare Python float, cedarpy would stringify it and this numeric
    # `when` clause would not behave as a real comparison. Proven here by
    # actually crossing the threshold via integer micro-USD.
    engine = _engine(tmp_path, "context.trace_cumulative_cost_usd_micros >= 500000")  # $0.50
    tracker = CostTracker()
    trace_id = "trace-c"

    assert _decide(engine, tracker, trace_id, None) is True

    tracker.record(trace_id=trace_id, scope_id=None, tokens=100, cost_usd_micros=750_000)  # $0.75

    assert _decide(engine, tracker, trace_id, None) is False


def test_span_budget_isolates_the_current_turn_from_the_rest_of_the_trace(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "context.span_cumulative_tokens >= 1000")
    tracker = CostTracker()
    trace_id = "trace-d"

    # Turn 1 spends heavily, but under its OWN span scope.
    tracker.record(trace_id=trace_id, scope_id="turn-1", tokens=1500, cost_usd_micros=0)
    # Turn 2 (a later, DIFFERENT model_call in the SAME trace) starts fresh
    # under its own scope_id -- a per-turn budget must not carry turn-1's
    # spend over, even though the trace-level total would.
    assert _decide(engine, tracker, trace_id, "turn-2") is True
    # But turn-1 itself (e.g. a tool_call still correlated to it) IS over.
    assert _decide(engine, tracker, trace_id, "turn-1") is False


def test_zero_spend_never_trips_a_positive_threshold(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "context.trace_cumulative_tokens >= 1")
    tracker = CostTracker()
    assert _decide(engine, tracker, "trace-e", None) is True
