# ADR 0010 — Cumulative cost/token tracking (trace and span scope only)

Status: accepted

## Context

A cost/budget policy ("deny once this trace has spent $5", "hold for review
past 50k tokens in one turn") needs a *cumulative* number Cedar can compare
against a threshold. Cedar itself cannot provide one: `PolicyEngine.evaluate()`
is stateless per call, by design (see engine.py's own module docstring on
default-deny and fail-closed semantics — none of that reasoning changes here).
Whatever computes a running total has to live outside Cedar and hand it to
`context` like any other fact.

Two constraints from the existing architecture shape where that total can
live:

- **The control plane is never on the decision path** (ADR 0009 restates this
  for approvals; it applies here identically). A live cost check cannot
  synchronously depend on control-plane reachability.
- **cedarpy has no native float/decimal context type.** `policy/engine.py`'s
  `_cedar_leaf()` stringifies a bare Python float rather than passing it as a
  Cedar `Long` — confirmed against a real failure (a tool call with a
  latitude/longitude argument), not assumed. A numeric `when` clause against a
  stringified dollar amount does not do what it looks like it does.

## Decision

**A new `CostTracker` (policy/cost_tracker.py) holds in-memory running totals,
scoped to TRACE and SPAN only.** Session (cross-trace) and daily (cross-session,
almost certainly cross-process in a scaled deployment) cumulative budgets are
explicitly out of scope for this ADR — they need state that survives a single
trace and, once there is more than one replica, a single process, which is a
real infrastructure decision (shared store vs. a documented single-replica
limitation) and not something to default into silently inside a policy
primitive. Revisit as its own ADR when that's needed.

**Dollar amounts are tracked as integer micro-USD (1,000,000 == $1), never a
float.** Cents was considered and rejected: at real model pricing (e.g.
gpt-4o-mini's $0.15/1M input tokens), a single call rounds to $0.00 in cents,
making a per-call-granularity threshold meaningless until many calls
accumulate. Token counts need no such projection — they are already integers.

**"Span" scope means one conversational turn: a model_call and whatever
tool_call(s) it triggers — not an arbitrary OTel span subtree.** MAF's
model_call and tool_call spans are documented (see maf.py's `_ChatCorrelation`)
to fire as sequential SIBLINGS correlated via an explicit `SpanContext`, not
ambient `start_as_current_span()` nesting — so "whatever OTel span is
currently active" is not a reliable proxy for "this turn" the way it is for
trace_id. The tracker's `scope_id` reuses that SAME existing correlation
(the model_call's own span id) rather than inventing a second nesting
mechanism. A wider "everything a sub-agent invocation spent, including nested
sub-agents" scope would need real ambient-span nesting keyed off the
THIRD-PARTY MAF instrumentor's AGENT-kind spans (not something this package
emits) — left for future work if that granularity turns out to matter.

**Bounded by LRU eviction, not a required "trace ended" hook.** There is no
single reliable, framework-agnostic "this trace just finished" callback across
every in-process integration today. `CostTracker.end_trace()` exists for a
caller that DOES know its own boundary, but nothing depends on it for
correctness — `max_traces` (default 10,000) bounds memory in a long-lived
process regardless.

**Context injection reuses the existing `extra_context` parameter on
`GovernanceHook.evaluate()` — hooks.py itself is unchanged.** Tier-2
content-check scanners already establish this exact pattern (compute
something Snapshot/Caller alone can't, hand it in as flat top-level context
keys). Each framework adapter (maf.py, adk.py) owns its own module-level
`CostTracker` instance and its own trace_id/scope_id derivation, rather than
`GovernanceHook` holding cost state generically — trace_id is safely derivable
from the ambient OTel span (same as `_set_span_attributes` already does), but
scope_id is not (see the sibling-span point above), so a generic
implementation in hooks.py would get span scope wrong for a tool_call. Keeping
it framework-specific also means an adapter that doesn't wire it (the
gateway, which has zero OTel wiring today; langgraph.py) simply doesn't get
cumulative cost context — an explicit gap, not a silently wrong one.

## Consequences

- A cost policy can only react to what has ALREADY been spent when a new call
  is evaluated — it cannot know this call's own cost before the response
  comes back (usage is a post-response fact). "Deny once over budget" means
  the call that pushes the total over the line still executes; the NEXT one
  is denied. This is inherent to cost being learned after the fact, not a
  gap in this implementation.
- In-memory-only means a process restart resets every trace's cumulative
  total. Acceptable for trace/span scope (a trace does not meaningfully
  survive a restart anyway) — would NOT be acceptable if this were extended
  to session/daily without also solving persistence, which is exactly why
  that extension needs its own ADR.
- Only `parapetai_agent.maf` and `parapetai_agent.adk` are wired in this pass.
  The gateway (no OTel wiring) and `langgraph.py` are explicitly not — a real
  scope cut for this pass, not an oversight.
