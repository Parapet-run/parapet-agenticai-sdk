# LangGraph / LangChain

```bash
pip install "parapetai-agent[langgraph]"
```

`ParapetAgentMiddleware` is a real `langchain.agents.middleware.AgentMiddleware`
— construction-time, genuinely blocking, registered the same way any other
`AgentMiddleware` is:

```python
from langchain.agents import create_agent
from parapetai_agent.langgraph import build_middleware

agent = create_agent(
    model,
    tools=[lookup_order, hr_lookup],
    middleware=[build_middleware(policy_dir="./policies")],
)
```

Full parameter reference: [`ParapetAgentMiddleware` / `build_middleware()` API](../reference/langgraph.md).

## Why `AgentMiddleware`, not `create_react_agent`

`langgraph.prebuilt.create_react_agent` — the pattern used elsewhere in this
SDK's own test/example code
(`tests/test_conformance_frameworks.py`'s `TestLangGraph`,
`examples/same_prompt_every_framework/adapters/langgraph.py`) — predates
`middleware=` support entirely and cannot block a call before it executes.
`langchain.agents.create_agent` (the current, non-deprecated construction
API, confirmed live against `langchain==1.3.18`/`langgraph==1.2.11`) has a
real `middleware: Sequence[AgentMiddleware] = ()` parameter, and
`AgentMiddleware.wrap_model_call`/`wrap_tool_call` receive a `handler`
callable the middleware explicitly chooses whether to invoke — the same
"raise before calling the real thing" shape MAF's own `ChatMiddleware`/
`FunctionMiddleware` use. This is why the `langgraph` extra depends on the
full `langchain` package, not just `langgraph`/`langchain-core` —
`AgentMiddleware` lives in `langchain.agents.middleware`.

## What it governs

All three stages `GovernedAgent`/`GovernedRunner` govern for MAF/ADK:

- **Pre-model** (`wrap_model_call`, before `handler(request)`): a Cedar
  `model_call` decision. A deny raises `GovernanceDenied` before the model
  is ever invoked.
- **Tool call** (`wrap_tool_call`, before `handler(request)`): a Cedar
  `tool_call` decision, by tool name + arguments. A deny raises before the
  tool body ever runs.
- **Post-model** (`wrap_model_call`, after `handler(request)` returns): a
  Cedar `post`-stage decision against the model's own response text. A
  deny raises before the response reaches the caller.

This is real, tested coverage — closing all three gaps the SDK's older,
generic `Governor.tool` path (still documented below) leaves open.

## "Doesn't Integrations already give you a consistent API across providers?"

Yes, and it's worth understanding why that didn't become the design here.
`langchain-openai`, `langchain-anthropic`, and the ~50+ other integration
packages all implement `BaseChatModel`, one consistent interface
(`.invoke()`/`.ainvoke()`/`.stream()`/`.bind_tools()`) regardless of
provider — and LangGraph's `create_react_agent`/`create_agent` and
DeepAgents' `create_deep_agent` all consume it directly as `model=`. A
`BaseChatModel`-wrapping adapter would be framework-agnostic (bare
LangChain, LangGraph, DeepAgents alike) for the model-call stages — but it
cannot cover tool-call authorization at all, since a tool call executes in
a separate node (`ToolNode`/the agent executor), never inside the chat
model client. `AgentMiddleware` covers **both** model-call and tool-call
stages at the one place `create_agent`/`create_deep_agent` both already
accept a `middleware=` list, without needing a second, complementary
interception point — which is why it's the design this module uses instead.

## Known gaps

Deliberately deferred rather than silently half-built — see
`parapetai_agent/langgraph.py`'s own module docstring for the full,
current list:

- **No tier-2 content-checks/groundedness/judge scanning, no ALTER
  support.** `build_middleware()` has no `alter_transforms=` parameter —
  accepting it and doing nothing would be worse than not accepting it.
  The core `check_input`/`check_output`-equivalent Cedar gating (above) is
  implemented; the additional scanners a control-plane bundle can carry
  are not wired in yet.
- **No per-call OTel span with OpenInference attributes** (token counts,
  a `parapetai.model_call` span). Decisions still reach OTel as LogRecords
  via `governance_runtime.audit()`, the same sink MAF/ADK use — what's
  missing is the additional per-call span, not decision observability.
- **Streaming has not been verified** against a live `.astream()`/
  `.stream()` call — treat as unverified, the same caution `adk.py`'s own
  docstring applied to its streaming claim before that was checked.

## The older, generic path — still valid, now complementary

Before this adapter existed, [`Governor.tool`](../reference/governor.md)
was the only way to govern a LangGraph tool call — a decorator that
authorizes one function by name + kwargs:

```python
from langchain_core.tools import tool as lc_tool
from langgraph.prebuilt import create_react_agent
from parapetai_agent import Governor

gov = Governor.from_policy_dir("./policies")

@gov.tool
def lookup_order(order_id: str) -> str: ...

agent = create_react_agent(model, tools=[lc_tool(lookup_order)])
```

This still works — `Governor` didn't change — and remains the right choice
if you specifically want `create_react_agent` (not `create_agent`) or the
lighter `langchain-core`-only dependency footprint over the full
`langchain` package. It's tool-call-only (no pre/post-model gating, no
ambient identity), which is exactly what `ParapetAgentMiddleware` above
was built to close. Two runnable examples show the manual workarounds this
older path needs to approach parity:
[`examples/langgraph_tool_calling.py`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/examples/langgraph_tool_calling.py)
(explicit `check_input`/`check_output` calls) and
[`examples/langgraph_identity_scoped.py`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/examples/langgraph_identity_scoped.py)
(identity via `RunnableConfig`) — both still valid, both now superseded in
capability by `build_middleware()` for anyone who can take the
`langchain>=1.3` dependency.

## Identity

Reuses `scoped_data.governed_identity()` unchanged — the same ambient
context MAF/ADK read:

```python
from parapetai_agent.scoped_data import governed_identity

with governed_identity(claims={"org": "Sales", "name": "Tony"}):
    agent.invoke({"messages": [{"role": "user", "content": prompt}]})
```

No LangGraph-specific identity variant (an equivalent to MAF's
`credential=` for azure-identity) exists yet — add one only if a real need
surfaces, per this module's own "don't build it speculatively" discipline.

## Next

- [`ParapetAgentMiddleware` / `build_middleware()` full API reference](../reference/langgraph.md)
- [`examples/langgraph_tool_calling.py`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/examples/langgraph_tool_calling.py) / [`examples/langgraph_identity_scoped.py`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/examples/langgraph_identity_scoped.py) — the older `Governor.tool` path
- [`Governor` reference](../reference/governor.md) — the framework-neutral fallback this module's own tests also exercise
- [Frameworks overview](overview.md) — support matrix across all integrations, in-process and gateway
