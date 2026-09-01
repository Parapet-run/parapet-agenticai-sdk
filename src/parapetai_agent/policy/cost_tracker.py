"""In-memory cumulative token/cost tracking, scoped to TRACE and SPAN only.

See docs/adr/0010-cumulative-cost-tracking.md for the design rationale this
module implements. Short version:

- Cedar has no memory across calls -- it evaluates one request's `context`
  at a time. A cumulative budget policy (e.g. "deny once this trace has
  spent $5") needs something stateful computing the running total and
  handing it to Cedar as an ordinary context value; this module is that
  state, and policy/engine.py's PolicyEngine stays exactly as stateless as
  it always was.

- Scoped to TRACE (every governed call under one Agent.run()) and SPAN
  (one conversational turn: a model_call plus whatever tool_call(s) it
  triggers) only. Session- and day-level cumulative budgets need state that
  survives a single trace and, in a horizontally-scaled deployment, a
  single process -- deliberately out of scope here; see the ADR for why
  that is a real infra decision (shared store vs. documented single-replica
  caveat) rather than something to default into silently.

- Dollar amounts are tracked as integer MICRO-USD (1_000_000 == $1), never
  a float. cedarpy has no native float/decimal context type -- a bare
  Python float gets stringified before reaching Cedar (see
  policy/engine.py's _cedar_leaf docstring), which silently breaks a
  numeric `when` clause. Cents was considered and rejected: a single call
  against a cheap model (e.g. gpt-4o-mini's $0.15/1M input tokens) rounds
  to $0.00 in cents, making a per-call threshold useless until many calls
  accumulate. Token counts are already integers and need no such
  projection.

- Bounded by an LRU eviction, not an explicit "trace ended" hook. Finding
  a reliable, framework-agnostic "this trace is over" callback across
  every in-process integration (MAF's own AGENT-kind spans come from a
  THIRD-PARTY instrumentor, not this package -- see maf.py's module
  docstring) is its own can of worms; a caller that DOES know its trace
  boundary can still call end_trace() as a courtesy, but nothing here
  depends on it for correctness or bounded memory.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from opentelemetry.trace import SpanContext


def span_ids(span_context: SpanContext) -> tuple[str, str] | None:
    """(trace_id, span_id) as lowercase hex, or None when span_context is
    the invalid, all-zero context a no-op tracer produces (no OTel
    TracerProvider configured yet). Shared by every in-process framework
    adapter (maf.py, adk.py) that derives CostTracker keys from an ambient
    or correlated SpanContext, so trace_id/span_id are formatted identically
    everywhere rather than each adapter re-deriving its own hex convention."""
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


# Cap on distinct trace_ids tracked at once -- oldest (by last-touched, not
# creation) evicted first once exceeded. Bounds memory in a long-lived
# server process without needing every caller to remember to clean up.
# Generous: even at this ceiling, each entry is a few dozen bytes.
_DEFAULT_MAX_TRACES = 10_000


@dataclass(slots=True)
class _Totals:
    tokens: int = 0
    cost_usd_micros: int = 0


class CostTracker:
    """Cumulative token/cost totals for TRACE and SPAN scope, in-memory.

    One instance is meant to be shared (module-level singleton, see
    maf.py/adk.py) across every call in a process -- trace_id is already
    globally unique (random hex per OTel trace), so sharing is safe and
    avoids each framework adapter inventing its own instance.

    Thread/async-safe: usage can arrive from whichever event loop or
    thread the LLM call happened to complete on, so all mutation goes
    through one lock. Reads (context_for) take the same lock rather than a
    separate read path -- call volume here is per-LLM-call, not hot-loop,
    so a single lock is not a real contention concern.
    """

    def __init__(self, max_traces: int = _DEFAULT_MAX_TRACES) -> None:
        self._max_traces = max_traces
        self._lock = threading.Lock()
        # OrderedDict as an LRU: move_to_end() on touch, popitem(last=False)
        # to evict the least-recently-touched trace once over capacity.
        self._trace: OrderedDict[str, _Totals] = OrderedDict()
        self._scope: dict[tuple[str, str], _Totals] = {}

    def record(
        self, *, trace_id: str, scope_id: str | None, tokens: int, cost_usd_micros: int
    ) -> None:
        """Call once, after one model_call's usage is known (post-response
        -- there is no usage to record before the call runs). Credits the
        trace total always, and the (trace_id, scope_id) total when
        scope_id is given -- a caller with no meaningful span-scope
        boundary (e.g. no ambient OTel span) can pass scope_id=None and
        still get trace-level tracking."""
        if tokens <= 0 and cost_usd_micros <= 0:
            return
        with self._lock:
            self._touch_trace(trace_id)
            t = self._trace[trace_id]
            t.tokens += tokens
            t.cost_usd_micros += cost_usd_micros
            if scope_id is not None:
                key = (trace_id, scope_id)
                s = self._scope.setdefault(key, _Totals())
                s.tokens += tokens
                s.cost_usd_micros += cost_usd_micros

    def context_for(self, *, trace_id: str | None, scope_id: str | None) -> dict[str, int]:
        """Cumulative totals so far -- NOT including whatever call is
        about to be evaluated, since it hasn't happened yet (a policy can
        only react to what has already been spent, never predict this
        call's own cost before the response comes back). All four keys
        are always present, zero when nothing has been recorded yet, so a
        Cedar `when` clause never needs `context has trace_cumulative_
        tokens` -- the field is always there."""
        zero = {
            "trace_cumulative_tokens": 0,
            "trace_cumulative_cost_usd_micros": 0,
            "span_cumulative_tokens": 0,
            "span_cumulative_cost_usd_micros": 0,
        }
        if trace_id is None:
            return zero
        with self._lock:
            t = self._trace.get(trace_id)
            s = self._scope.get((trace_id, scope_id)) if scope_id is not None else None
            return {
                "trace_cumulative_tokens": t.tokens if t else 0,
                "trace_cumulative_cost_usd_micros": t.cost_usd_micros if t else 0,
                "span_cumulative_tokens": s.tokens if s else 0,
                "span_cumulative_cost_usd_micros": s.cost_usd_micros if s else 0,
            }

    def end_trace(self, trace_id: str) -> None:
        """Drop all state for one trace (its total and every scope
        recorded under it). Optional -- the LRU bound is what guarantees
        memory stays bounded even if a caller never calls this -- but a
        caller that DOES know exactly when a trace finished should still
        call it, so a long-running process doesn't carry dead traces
        until they age out of the LRU."""
        with self._lock:
            self._trace.pop(trace_id, None)
            for key in [k for k in self._scope if k[0] == trace_id]:
                del self._scope[key]

    def _touch_trace(self, trace_id: str) -> None:
        """Must be called with self._lock held. Creates the trace entry if
        new, marks it most-recently-used, and evicts the least-recently-used
        entry (and its scope entries) if that pushes us over max_traces."""
        if trace_id in self._trace:
            self._trace.move_to_end(trace_id)
            return
        self._trace[trace_id] = _Totals()
        if len(self._trace) > self._max_traces:
            evicted, _ = self._trace.popitem(last=False)
            for key in [k for k in self._scope if k[0] == evicted]:
                del self._scope[key]
