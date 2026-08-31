# Microsoft Agent Framework

```bash
pip install "parapetai-agent[maf]"
```

`GovernedAgent` is a drop-in replacement for `agent_framework.Agent` —
swap the import, keep everything else:

```python
from parapetai_agent import GovernedAgent  # was: from agent_framework import Agent
from parapetai_agent.scoped_data import governed_identity  # or: from parapetai_agent.maf import governed_identity

async with GovernedAgent(
    client=client,
    name="workplace-agent",
    instructions="You are a workplace assistant with access to internal tools.",
    tools=[salesforce_lookup, hr_lookup],
    policy_dir="./policies",
) as agent:
    with governed_identity(claims={"org": "Sales", "name": "Tony"}):
        result = await agent.run("Look up the ACME opportunity")
```

Full parameter reference: [`GovernedAgent` API](../reference/governed-agent.md).

## What actually changed

Nothing about the agent's own tools, instructions, or model client. Two
additions:

1. The `GovernedAgent` import (or, if you can't subclass `Agent`,
   [`build_middleware()`](../reference/governed-agent.md#build_middleware)
   + `middleware=[chat_mw, func_mw]`).
2. One [`governed_identity()`](../reference/governed-identity.md) context
   manager per call, asserting which end user the request is on behalf
   of.

Cedar decides the rest — a policy scoped to `org` permits
`salesforce_lookup` only for `org=Sales` and `hr_lookup` only for
`org=HR`, and denies each identity the other's tool.

## How a deny surfaces

**Asymmetric by design** — verified against real behavior, not assumed,
because MAF's own middleware pipeline constrains what's possible
differently at each layer:

- **Model call**: a pre- or post-stage deny **raises**
  `GovernanceDenied` for real — the underlying HTTP call to the model
  never fires.
- **Tool call**: a deny does **not** raise. It substitutes a synthetic
  string result — `context.result = f"GOVERNANCE_DENIED: {reason}"` — and
  never calls the actual tool. This is deliberate: raising from
  `FunctionMiddleware.process()` gets silently caught by MAF's own
  function-invocation loop and converted into a generic tool-error
  result, which would throw away the `Decision` detail. Folding the
  denial into the result string instead keeps that detail visible.

```python
result = await agent.run(prompt)
if "GOVERNANCE_DENIED" in result.text:
    # the tool never ran; the model saw the denial reason as if it were
    # a tool error and (usually) explains it in its own final answer
    ...
```

If you need the structured [`Decision`](../reference/decision.md) itself
rather than parsing text, use the `on_decision` callback (see
[`Decision`](../reference/decision.md#getting-a-decision-out-of-a-call))
— it fires for every decision regardless of which layer produced it.

## Streaming

Documented directly as "the one place this is not a real gate": MAF only
exposes a finalized-stream hook that fires **after** every chunk has
already reached the caller. When a response is streamed, the post-call
Cedar check runs against the accumulated finalized text once streaming
completes — but by then, delivery already happened. A deny/alter on a
streamed response can only be **logged as a warning**
(`post_call_would_deny_streaming` / `post_call_would_alter_streaming`); it
cannot block or rewrite anything.

Model-call (pre) and tool-call decisions are unaffected by this — they
gate a request *before* it goes out, so there's nothing to buffer. It's
specifically the **post**-stage (`check_output`-equivalent) gate on a
*streamed* model response that can't truly block.

If blocking a streamed response before delivery matters for your use
case, compare with [ADK's streaming behavior](adk.md#streaming), which
buffers per-chunk and evaluates before the final chunk is delivered.

## Identity

Two ways to assert who's calling:

```python
# End-user claims/roles, or a raw bearer token
with governed_identity(claims={"org": "Sales", "name": "Tony"}):
    ...

# An azure-identity credential -- MAF-specific, since FoundryChatClient
# commonly takes exactly this kind of credential
from parapetai_agent.maf import governed_identity as maf_governed_identity
with maf_governed_identity(credential=AzureCliCredential()):
    ...
```

See [`governed_identity` reference](../reference/governed-identity.md) for
the full parameter list of both variants — MAF ships a richer one with
`credential=`/`scope=` support that ADK's re-export doesn't have.

## Next

- [`GovernedAgent` full API reference](../reference/governed-agent.md)
- [`governed_identity` reference](../reference/governed-identity.md)
- Verified end to end against a real conformance test — see
  [Frameworks overview](overview.md) for how MAF's in-process adapter
  differs from the gateway's (separate, `unknown`) MAF row.
