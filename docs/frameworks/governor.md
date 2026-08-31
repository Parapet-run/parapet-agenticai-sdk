# Governor (framework-neutral)

Use `Governor` when there's no dedicated adapter for your framework yet,
or when you'd rather keep the three governance checks fully explicit in
your own loop — LangGraph, CrewAI, the OpenAI Agents SDK, or a bare
`while` loop all use this.

## Construct once

```python
from parapetai_agent import Governor

# Fully local -- no network call, ever
gov = Governor.from_policy_dir("./policies")

# Or, backed by a control plane (refreshed in the background;
# every decision is still evaluated locally)
gov = Governor.from_control_plane(policy_dir="./policies")
```

Full parameter reference: [`Governor` API](../reference/governor.md).

## Call from wherever your loop fires

```python
# Before the model sees the prompt
decision = gov.check_input(prompt_text)

# Before a tool actually runs
decision = gov.authorize_tool(tool_name, tool_args)

# Before the model's answer reaches the caller
decision = gov.check_output(response_text)
```

Each call **raises by default** (`GovernanceDenied` on deny,
`GovernanceReviewRequired` on a held review) rather than returning
something you might forget to check — pass `raise_on_deny=False` on any
call to get the [`Decision`](../reference/decision.md) back directly and
branch yourself.

## Scoping to a caller

Two identity layers exist, and they answer different questions:

- **The process's own identity** (`Caller`, set once at construction) —
  "which agent is this."
- **The end user's identity**, passed per call via `roles=`/`claims=` on
  `check_input()`/`authorize_tool()`/`check_output()` — "who is this
  request on behalf of."

```python
from parapetai_agent.identity import Caller

gov = Governor.from_policy_dir("./policies", caller=Caller(agent_id="weather-bot"))

decision = gov.authorize_tool(
    "get_weather", {"city": "Paris"},
    claims={"org": "Ops", "name": "Priya"},
)
```

A Cedar policy scoped to `context.identity_claims.org` sees exactly what
was passed in `claims=` for that one call — nothing ambient, nothing left
over from a previous call.

## Async / streaming

Cedar evaluation itself is synchronous and blocking — there's no `async`
anywhere in the policy engine. `Governor`'s three checks are plain sync
functions you call at the right point in your own loop; how (or whether)
your framework streams a model response is entirely up to your own
integration code, since `Governor` has no framework loop of its own to
hook into. If you need streaming-aware pre/post gating, see how
[`GovernedRunner`](adk.md#streaming) (buffers per-chunk, evaluates the
final one before delivery) and [`GovernedAgent`](maf.md#streaming) (can
only audit-after-the-fact on a stream) each handle it differently, for
comparison.

## Review approvals

```python
from parapetai_agent import GovernanceReviewRequired

try:
    gov.authorize_tool("wire_transfer", {"amount": 50000})
except GovernanceReviewRequired as held:
    if gov.wait_for_approval(held, timeout=300.0):
        ...  # proceed exactly once
```

See [Exceptions](../reference/exceptions.md#resolving-a-held-call) and
[ADR 0009](../adr/0009-approval-loop.md) for the full design.

## Next

- [`Governor` full API reference](../reference/governor.md)
- [`Decision`](../reference/decision.md) — everything a check returns
- [Quickstart](../getting-started/quickstart.md) — a runnable first example
