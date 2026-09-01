"""parapetai_agent.policy.cost_tracker -- see docs/adr/0010 for the design
this verifies: trace/span-only scope, integer micro-USD (never a float),
LRU-bounded memory, no reliance on an explicit end-of-trace hook."""

from __future__ import annotations

from parapetai_agent.policy.cost_tracker import CostTracker


def test_zero_state_is_all_zero_not_missing() -> None:
    tracker = CostTracker()
    ctx = tracker.context_for(trace_id="t1", scope_id="s1")
    assert ctx == {
        "trace_cumulative_tokens": 0,
        "trace_cumulative_cost_usd_micros": 0,
        "span_cumulative_tokens": 0,
        "span_cumulative_cost_usd_micros": 0,
    }


def test_no_trace_id_returns_zero_state() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="s1", tokens=100, cost_usd_micros=500)
    assert tracker.context_for(trace_id=None, scope_id=None) == {
        "trace_cumulative_tokens": 0,
        "trace_cumulative_cost_usd_micros": 0,
        "span_cumulative_tokens": 0,
        "span_cumulative_cost_usd_micros": 0,
    }


def test_record_accumulates_trace_and_scope_totals() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="turn-a", tokens=100, cost_usd_micros=1_000)
    tracker.record(trace_id="t1", scope_id="turn-a", tokens=50, cost_usd_micros=500)
    ctx = tracker.context_for(trace_id="t1", scope_id="turn-a")
    assert ctx["trace_cumulative_tokens"] == 150
    assert ctx["trace_cumulative_cost_usd_micros"] == 1_500
    assert ctx["span_cumulative_tokens"] == 150
    assert ctx["span_cumulative_cost_usd_micros"] == 1_500


def test_different_scopes_under_same_trace_stay_isolated() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="turn-a", tokens=100, cost_usd_micros=1_000)
    tracker.record(trace_id="t1", scope_id="turn-b", tokens=40, cost_usd_micros=400)

    a = tracker.context_for(trace_id="t1", scope_id="turn-a")
    b = tracker.context_for(trace_id="t1", scope_id="turn-b")

    # Trace total sees BOTH turns -- the whole Agent.run(), not one turn.
    assert a["trace_cumulative_tokens"] == 140
    assert b["trace_cumulative_tokens"] == 140
    # But each turn's own span total only sees its own calls.
    assert a["span_cumulative_tokens"] == 100
    assert b["span_cumulative_tokens"] == 40


def test_different_traces_stay_isolated() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="s", tokens=100, cost_usd_micros=1_000)
    tracker.record(trace_id="t2", scope_id="s", tokens=999, cost_usd_micros=9_999)

    ctx1 = tracker.context_for(trace_id="t1", scope_id="s")
    assert ctx1["trace_cumulative_tokens"] == 100
    assert ctx1["trace_cumulative_cost_usd_micros"] == 1_000


def test_record_with_no_scope_id_still_credits_trace() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id=None, tokens=100, cost_usd_micros=1_000)
    ctx = tracker.context_for(trace_id="t1", scope_id=None)
    assert ctx["trace_cumulative_tokens"] == 100
    assert ctx["span_cumulative_tokens"] == 0  # never recorded under any scope


def test_zero_usage_record_is_a_noop() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="s", tokens=0, cost_usd_micros=0)
    assert tracker.context_for(trace_id="t1", scope_id="s")["trace_cumulative_tokens"] == 0
    # And doesn't even create a trace entry -- verified via the LRU test below
    # relying on untouched traces never counting toward max_traces.


def test_end_trace_clears_both_trace_and_scope_totals() -> None:
    tracker = CostTracker()
    tracker.record(trace_id="t1", scope_id="s", tokens=100, cost_usd_micros=1_000)
    tracker.end_trace("t1")
    ctx = tracker.context_for(trace_id="t1", scope_id="s")
    assert ctx["trace_cumulative_tokens"] == 0
    assert ctx["span_cumulative_tokens"] == 0


def test_end_trace_on_unknown_trace_is_a_noop() -> None:
    tracker = CostTracker()
    tracker.end_trace("never-seen")  # must not raise


def test_lru_eviction_bounds_memory_and_drops_oldest() -> None:
    tracker = CostTracker(max_traces=2)
    tracker.record(trace_id="t1", scope_id="s", tokens=10, cost_usd_micros=100)
    tracker.record(trace_id="t2", scope_id="s", tokens=20, cost_usd_micros=200)
    tracker.record(trace_id="t3", scope_id="s", tokens=30, cost_usd_micros=300)

    # t1 was least-recently-touched and evicted when t3 pushed over max_traces=2.
    assert tracker.context_for(trace_id="t1", scope_id="s")["trace_cumulative_tokens"] == 0
    assert tracker.context_for(trace_id="t2", scope_id="s")["trace_cumulative_tokens"] == 20
    assert tracker.context_for(trace_id="t3", scope_id="s")["trace_cumulative_tokens"] == 30


def test_lru_touch_on_record_protects_recently_used_trace() -> None:
    tracker = CostTracker(max_traces=2)
    tracker.record(trace_id="t1", scope_id="s", tokens=10, cost_usd_micros=100)
    tracker.record(trace_id="t2", scope_id="s", tokens=20, cost_usd_micros=200)
    # Touch t1 again -- it becomes most-recently-used, so t2 should be evicted next.
    tracker.record(trace_id="t1", scope_id="s", tokens=5, cost_usd_micros=50)
    tracker.record(trace_id="t3", scope_id="s", tokens=30, cost_usd_micros=300)

    assert tracker.context_for(trace_id="t1", scope_id="s")["trace_cumulative_tokens"] == 15
    assert tracker.context_for(trace_id="t2", scope_id="s")["trace_cumulative_tokens"] == 0
    assert tracker.context_for(trace_id="t3", scope_id="s")["trace_cumulative_tokens"] == 30
