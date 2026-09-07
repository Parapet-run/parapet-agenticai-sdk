# Cumulative cost & token tracking

Since **0.6.0**. Cedar's `PolicyEngine.evaluate()` is stateless per call —
it has no memory of what a trace has already spent. `policy/cost_tracker.py`
supplies that memory as an ordinary in-memory running total, handed to
Cedar's `context` like any other fact, so a policy can compare against a
threshold:

```cedar
forbid(principal, action, resource)
when { context.trace_cumulative_cost_usd_micros > 5000000 };   // > $5 this trace

forbid(principal, action == Action::"tool_call", resource)
@action("review")
when { context.span_cumulative_tokens > 50000 };                // hold past 50k tokens this turn
```

See [ADR 0010](../adr/0010-cumulative-cost-tracking.md) for the full design
rationale.

## Context fields

Four fields, always present (zero when nothing has been recorded yet for
that scope — a Cedar `when` clause never needs `context has
trace_cumulative_tokens` to guard against a missing key):

| Field | Meaning |
|---|---|
| `trace_cumulative_tokens` | Total tokens (prompt + completion) across every model call in this trace so far. |
| `trace_cumulative_cost_usd_micros` | Total estimated cost across the trace so far, in **integer micro-USD** (`1_000_000 == $1.00`). |
| `span_cumulative_tokens` | Same, scoped to the current span (one conversational turn — a model call plus whatever tool call(s) it triggers) rather than the whole trace. |
| `span_cumulative_cost_usd_micros` | Same, span-scoped. |

**Never a float.** `cedarpy` has no native float/decimal context type — a
bare Python float silently stringifies before reaching Cedar, which
breaks a numeric `when` clause without raising anything. Divide by
`1_000_000` yourself if you want to reason in dollars.

**These totals do NOT include the call currently being evaluated** — a
policy can only react to what has already been spent, never predict the
cost of the call it's deciding on before the response comes back.

## Scope: trace and span only

- **Trace** = every governed call under one top-level run (e.g. one
  `Agent.run()`).
- **Span** = one conversational turn — a model call plus whatever tool
  call(s) it triggers.

**Session (cross-trace) and daily (cross-session) cumulative budgets are
out of scope.** They need state that survives a single trace and, once
there's more than one replica, a single process — a real infrastructure
decision (shared store vs. a documented single-replica limitation), not
something to default into silently inside a policy primitive.

Bounded by an LRU eviction (10,000 distinct trace ids by default) as the
universal fallback — MAF/ADK have no reliable, framework-agnostic "trace
ended" callback to hook into, so they rely on it alone. LangGraph
(`after_agent`) and `Governor` (`trace()`'s own context-manager exit)
*do* know exactly when their trace ends and call `end_trace()` as a
courtesy on top of the same LRU bound. Memory stays bounded
either way; a caller that does know its own trace boundary can call
`CostTracker.end_trace(trace_id)` as a courtesy to free memory sooner.

## Which integrations get this, and how automatic it is

All four integration surfaces populate the same `context` fields — a
policy written against `context.trace_cumulative_*` works identically
regardless of which one you use — but how the trace/turn boundary is
found, and whether usage is learned automatically or has to be reported,
differs:

| Integration | Trace boundary | Usage source |
|---|---|---|
| MAF / ADK | The real model/tool call's own OTel `SpanContext`. | Learned automatically from the framework's own response object (`ChatResponse.usage_details`, etc.) — no opt-in flag, nothing to report. |
| LangGraph (`ParapetAgentMiddleware`) | `before_agent`/`after_agent` (one whole `agent.invoke()`/`ainvoke()` run) — no OTel span exists here yet, so these generate and correlate their own ids instead. | Learned automatically from each `AIMessage.usage_metadata`, when the model populates it (not every provider does — see [Corroboration](corroboration.md)'s "no signal ≠ no mismatch" caveat for the analogous limitation there). |
| [`Governor`](../frameworks/governor.md) | Explicit — wrap one whole run in `gov.trace()`. Without it, every `check_input()`/`authorize_tool()`/`check_output()` call is its own one-off trace (fields still always populated, never absent — they just don't accumulate past that one call). | **Must be reported.** `Governor` never sees the model's own response object (only text you hand `check_output()`), so it cannot learn usage on its own — pass `check_output(model=..., prompt_tokens=..., completion_tokens=...)`. |

For MAF/ADK specifically: every model-call decision (pre and post) and
every tool-call decision automatically carries all four fields once
cost/token usage is known — no opt-in flag required there either.

If you're integrating a framework none of the four cover, use
`policy/cost_tracker.CostTracker` directly (`new_trace_id()`/
`new_span_id()` for id generation with no OTel dependency, the same
helpers LangGraph's and `Governor`'s own tracking use) and thread its
`context_for()` output into your own `extra_context=`.

## Pricing

`policy/pricing.py` estimates cost from token counts using a built-in
`$/1,000,000 tokens` table for common models (GPT-4o/4.1 family, o3/o4-mini,
Claude Sonnet/Haiku/Opus, Gemini 2.5). Matched by exact model id first,
then longest-prefix, so a dated/tagged variant (`gpt-4o-2024-08-06`)
resolves to its base entry (`gpt-4o`).

**An unpriced model returns `None`, never `$0`** — a budget policy must
never silently treat an unknown, possibly-expensive model as free.

Override or extend the table with [`PARAPETAI_MODEL_PRICING`](env-vars.md):

```bash
export PARAPETAI_MODEL_PRICING='{"my-fine-tune": {"input": 1.0, "output": 3.0}}'
```

Deliberately the **same** variable name and shape the control plane's own
retrospective cost-panel rollup reads — set pricing overrides once and
both the live budget decision here and the dashboard rollup there agree.
Malformed JSON is ignored wholesale (falls back to defaults), not
half-applied.

## See also

- [ADR 0010](../adr/0010-cumulative-cost-tracking.md) — full design
  rationale, including why session/day-level budgets were deliberately
  left out of scope.
- [Environment variables](env-vars.md) — `PARAPETAI_MODEL_PRICING`.
