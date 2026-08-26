# Examples

## `authorize_tool_calls.py` — runs on the base install

Authorizes tool calls against the example Cedar bundle in `../policies` — no
model, no network, no control plane. Shows allow vs deny and the determining
policy.

```bash
pip install parapetai-agent
python examples/authorize_tool_calls.py
```

## Governing a real agent

Wrap a Microsoft Agent Framework agent (needs the `maf` extra and a model
endpoint), and every model and tool call becomes a governed decision:

```python
from parapetai_agent import GovernedAgent as Agent, GovernanceDenied

agent = Agent(
    name="support",
    instructions="Help the customer.",
    tools=[lookup_order],
    policy_dir="./policies",        # or control_plane_url=... for a signed bundle
)

try:
    result = await agent.run("Where is order A1001?")
except GovernanceDenied as denied:
    print("blocked:", denied.reason)
```

See the top-level `README.md` for identity binding, the control-plane API, and
OTel export.


---

# Framework examples

Each subfolder is self-contained: its own `README.md`, its own
`.env.example`/`.env` (copy one, don't share), its own `run_example.py`, and --
when using a control plane -- its own separately-provisioned
`PARAPETAI_AGENT_ID`, never one shared across examples.

**New here?** [`INTEGRATION_GUIDE.md`](INTEGRATION_GUIDE.md) is the consolidated
reference: every way to construct a `GovernedAgent`/`GovernedRunner`, invoke it
(streaming or not), and set identity, cross-referenced against whichever example
demonstrates each combination. The table below is the directory index; that page
is the "which pattern do I need, and how do they compose" answer.

## Local policy vs a real control plane

The two things these demonstrate are different, and it is worth knowing which
you are looking at:

* **`policy_dir=`** -- Cedar files you manage. Enough to see enforcement, and
  what the standalone `parapet-*-example` repos use.
* **`control_plane_url=` + `agent_secret=`** -- the agent's identity is
  provisioned by an operator, the PEP generates and registers its own Ed25519
  keypair on first run, pulls a signed bundle, and heartbeats. `maf_sample_01`,
  `maf_sample_02`, `adk_sample_01` and `maf_cli` take all three, so they
  exercise that path; the `.env.example` in each shows the shape.

Provisioning an agent is deliberately an OPERATOR action (the console, or
`POST /api/v1/agents` with a provisioning token). The SDK has no
self-provisioning path -- an agent able to mint its own identity would make the
Cedar principal meaningless. What IS automatic is the PEP's own keypair.

| Directory | Agent type | Demonstrates |
|---|---|---|
| [`maf_cli/`](maf_cli/README.md) | CLI / batch process | Several distinct invokers in one process run, each with their own identity via `parapetai_agent.identity_store`'s `set_identity()`/`use_identity()` -- no web server, no session cookies, nothing HTTP-shaped. |

Reach for `maf_webapp/`'s pattern when the process serves concurrent
end-user requests over HTTP; reach for `maf_cli/`'s pattern when it's a
script or batch job acting on behalf of one or more identities that need
to persist by a key you choose (a username, a thread id, a job id) rather
than by an inbound `Request`.

| Directory | Agent type | Demonstrates |
|---|---|---|

## `maf_sample_01` through `maf_sample_07` -- ported upstream MAF samples

Seven of [Microsoft Agent Framework's own `python/samples`](https://github.com/microsoft/agent-framework/tree/main/python/samples),
each ported with the same four-edit pattern -- see
[`docs/maf-integration-pattern.md`](../docs/maf-integration-pattern.md) for
the pattern itself (the thing worth learning), spelled out once instead
of seven times.

| Directory | Ported from (upstream) | Demonstrates |
|---|---|---|
| [`maf_sample_01/`](maf_sample_01/README.md) | `01-get-started/01_hello_agent.py` | Simplest possible agent -- non-streaming and streaming. |
| [`maf_sample_02/`](maf_sample_02/README.md) | `01-get-started/02_add_tools.py` | A `@tool`-decorated function tool, governed as a real Cedar `tool_call`. |
| [`maf_sample_03/`](maf_sample_03/README.md) | `01-get-started/03_multi_turn.py` | Multi-turn conversation via `AgentSession` -- each turn its own independent Cedar decision. |
| [`maf_sample_04/`](maf_sample_04/README.md) | `01-get-started/04_memory.py` | A `ContextProvider` for cross-turn memory in session state. |
| [`maf_sample_05/`](maf_sample_05/README.md) | `02-agents/tools/function_tool_with_approval.py` | Human-in-the-loop tool approval, composed with Cedar's own `tool_call` decision. |
| [`maf_sample_06/`](maf_sample_06/README.md) | `02-agents/middleware/function_based_middleware.py` | The framework's own function-based middleware, layered *after* Cedar's governance middleware. |
| [`maf_sample_07/`](maf_sample_07/README.md) | `02-agents/providers/openai/client_with_structured_output.py` | Pydantic structured output, on plain OpenAI routing instead of Azure OpenAI. |

## `adk_sample_01` -- Google ADK

Same "ported quickstart, minimal governance wiring" shape as the
`maf_sample_0N` samples above, for `parapetai_agent.adk` (Google ADK)
instead of `parapetai_agent.maf` (Microsoft Agent Framework). Only one
sample so far, unlike MAF's seven -- see `parapetai_agent/adk.py`'s own
module docstring for why the two integrations aren't identically shaped
(ADK's governable seam is `Runner`, not `Agent`) even though the
developer-facing wiring is equally minimal for both.

| Directory | Ported from (upstream) | Demonstrates |
|---|---|---|
| [`adk_sample_01/`](adk_sample_01/README.md) | [ADK Quickstart](https://google.github.io/adk-docs/get-started/quickstart/) | Simplest possible agent -- non-streaming and streaming, plus both identity sources (`Session.user_id`, opted in since it's a single-operator script, and `governed_identity()`). |
| [`adk_webapp/`](adk_webapp/README.md) | -- (hand-rolled, mirrors `maf_webapp/`) | A real FastAPI app fronting a governed ADK agent, with `IdentityMiddleware` lifting a verified JWT into Cedar per request -- the answer to "where does ADK get end-user identity from for a web deployment" (verified live against `google-adk`'s own source: nowhere, by default). |

More agent-type examples (e.g. a workflow/orchestration-style agent) may
be added here later, following the same shape: its own subfolder, its own
`README.md`, governed by the same `policies/`.
> `maf_webapp/` is **not** here. It is a demo we build and deploy to Azure from
> the control-plane repo (six scripts under `deploy/azure/` reference its
> Dockerfile), which makes it operated infrastructure rather than a sample to
> copy. Everything else that a customer would copy lives in this directory.