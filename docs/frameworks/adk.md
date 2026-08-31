# Google ADK

```bash
pip install "parapetai-agent[adk]"
```

`GovernedRunner` is a drop-in replacement for `google.adk.runners.Runner`
— swap the import, keep everything else. `InMemoryGovernedRunner` mirrors
`google.adk.runners.InMemoryRunner` for the common case of no real
session/artifact/memory backend:

```python
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from parapetai_agent.adk import GovernedRunner, governed_identity

root_agent = Agent(
    name="workplace_agent",
    model=model,
    instruction="You are a workplace assistant with access to internal tools.",
    tools=[salesforce_lookup, hr_lookup],
)
runner = GovernedRunner(
    agent=root_agent,
    app_name="demo",
    session_service=InMemorySessionService(),
    policy_dir="./policies",
)

with governed_identity(claims={"org": "Sales", "name": "Tony"}):
    async for event in runner.run_async(user_id="Tony", session_id=session_id, new_message=...):
        ...
```

Full parameter reference: [`GovernedRunner` API](../reference/governed-runner.md).

## Why the name is different from `GovernedAgent`

Not a naming inconsistency — ADK's own governable seam is
`Runner(plugins=[...])`, not the `Agent` class the way MAF's is
`Agent(middleware=[...])`. `GovernedRunner` subclasses `Runner` because
that's where ADK actually lets a plugin intercept a call; forcing a
shared `GovernedAgent` name across both frameworks would paper over that
they hook in at genuinely different layers.

## How a deny surfaces

`GovernedRunner` **never raises** — it uses ADK's own "early exit"
callback contract instead of exceptions, since that's what ADK's plugin
system natively supports:

- **Model call**: `before_model_callback`/`after_model_callback` return a
  synthetic `LlmResponse` — `error_code="governance_denied"`,
  `error_message=decision.reason`, and content
  `f"GOVERNANCE_DENIED: {decision.reason}"`. Returning a non-`None`
  `LlmResponse` from these callbacks is ADK's own documented
  response-substitution contract, not an exception.
- **Tool call**: `before_tool_callback`/`after_tool_callback` return a
  dict `{"error": f"GOVERNANCE_DENIED: {decision.reason}"}`, which ADK
  substitutes directly as the tool's result.

```python
async for event in runner.run_async(...):
    if event.error_code == "governance_denied":
        # the model call itself was blocked
        ...
```

For the structured [`Decision`](../reference/decision.md) itself rather
than parsing the synthetic response, use the `on_decision` callback (see
[`Decision`](../reference/decision.md#getting-a-decision-out-of-a-call))
— identical regardless of which layer produced it.

## Streaming

The opposite case from [MAF's](maf.md#streaming): `after_model_callback`
fires **once per streamed chunk** — a `partial=True` `LlmResponse` for
each fragment, then one final non-partial chunk with the complete
content. Every partial chunk is relayed unmodified (text is buffered as
it arrives), and the real Cedar evaluation runs **once**, on the final
chunk, against the accumulated text — **before** it's delivered. Because
this runs before delivery, the final chunk genuinely can be denied or
altered.

The tradeoff: earlier partial chunks were already relayed before enough
text existed to evaluate against — so a stream can start delivering
content before governance has seen the complete response, even though the
*final* chunk is a real, enforced gate. If your policy needs to guarantee
no partial content ever reaches the caller before a post-stage decision,
don't stream the response.

## Identity

```python
from parapetai_agent.adk import governed_identity

with governed_identity(claims={"org": "Sales", "name": "Tony"}):
    ...
```

This is a straight re-export of `parapetai_agent.scoped_data.governed_identity`
— no `credential=`/`scope=` support (that's MAF-specific, for
azure-identity credentials). See the [`governed_identity` reference](../reference/governed-identity.md)
for the full parameter list.

### `trust_session_user_id`

ADK's `Session.user_id` is a plain, **unverified** string every
`run_async()` call must supply — but ADK itself never authenticates it.
`GovernedRunner` does **not** let it flow into Cedar's `identity_claims`
by default (`trust_session_user_id=False`), because that would make
identity-gated policies silently *stricter* for ADK than for MAF (which
has no equivalent ambient field to even opt into). Set it `True` only
when your deployment's own source for `user_id` is already trusted. See
the [full parameter reference](../reference/governed-runner.md#trust_session_user_id-the-one-adk-specific-parameter).

## Next

- [`GovernedRunner` full API reference](../reference/governed-runner.md)
- [`governed_identity` reference](../reference/governed-identity.md)
- Verified end to end against a real conformance test — see
  [Frameworks overview](overview.md) for how ADK's in-process adapter
  differs from the gateway's (separate, `unknown`) ADK row.
