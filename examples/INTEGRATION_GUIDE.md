# Integrating `parapetai-agent` into your agent — the complete reference

One document, all the combinations. Each subsection below links to the
specific `examples/` subdirectory that runs it, so nothing here is
theoretical -- every pattern described has a working, tested version you
can copy from. `docs/maf-integration-pattern.md` covers the *why* behind
each design choice in more depth; this document is the *what exists and
how do I combine it* reference.

## 1. What's actually supported (correcting a common assumption)

There is **one** framework integration, not several: Microsoft Agent
Framework (`agent_framework`). Everything else people think of as
separate integrations is a dimension of *that one* integration, because
`parapetai_agent.maf`'s governance middleware
(`ParapetChatMiddleware`/`ParapetFunctionMiddleware`) subclasses
`agent_framework`'s own generic `ChatMiddleware`/`FunctionMiddleware` --
it operates on `ChatContext`/`FunctionInvocationContext`, never on a
specific client or tool-source class. Confirmed, not assumed:
`FoundryChatClient`'s own MRO is `FoundryChatClient ->
RawFoundryChatClient -> RawOpenAIChatClient -> BaseChatClient` -- it
*is* an OpenAI-shaped client under the hood, sharing the exact same
`agent-framework-core` foundation every other client sits on.

**Chat clients** (swap `client=` freely, governance is identical either way):

| Client | Verified as | Needs |
|---|---|---|
| `agent_framework.openai.OpenAIChatCompletionClient()` | `openai` or `azure` (via `.azure_endpoint`, not class name) | `OPENAI_API_KEY` or `AZURE_OPENAI_ENDPOINT`/`_API_KEY` |
| `agent_framework.foundry.FoundryChatClient(credential=...)` | `azure` (falls through to class-name table) | `FOUNDRY_PROJECT_ENDPOINT`/`FOUNDRY_MODEL` + a real Azure identity (see §3) |
| `agent_framework.anthropic.AnthropicClient()` | `anthropic` | `ANTHROPIC_API_KEY` |
| `agent_framework.gemini.GeminiChatClient()` | `gemini` | `GEMINI_API_KEY` |

(See `conformance/matrix.yaml`'s `model_providers` for exactly what
"verified" means per entry -- most are `verified_synthetic`, meaning
provider identification was proven with a real client instance, not that
a live call was made against every provider in this environment.)

**Tool sources** (mix freely on one agent, same governance either way):

| Source | Verified as |
|---|---|
| Plain Python functions (`tools=[my_func]`) | `native` |
| `MCPStreamableHTTPTool` | `mcp-streamable-http` |
| `MCPStdioTool` | `mcp-stdio` |
| `MCPWebsocketTool` | `mcp-websocket` (upstream-deprecated transport, still governed identically) |

None of the above changes how you construct `GovernedAgent` or how you
invoke it -- that's the whole point of the client/tool-source-agnostic
middleware design. The rest of this document is about the axes that
*do* vary: construction, invocation, and identity.

## 2. Constructing a `GovernedAgent`

```python
from parapetai_agent import GovernedAgent

agent = GovernedAgent(
    client=...,             # any client from the table above -- required
    name="...",              # agent_framework's own Agent kwarg -- required
    instructions="...",      # agent_framework's own Agent kwarg -- required
    tools=[...],              # optional, any mix of the tool sources above
    context_providers=[...],  # optional, agent_framework's own -- passes through unchanged
    middleware=[...],         # optional, YOUR OWN agent_framework middleware --
                               # runs AFTER governance, never instead of it

    # ---- everything below is parapetai-agent's own integration surface, and
    # ---- every single one of these is OPTIONAL ----
    policy_dir=None,               # None -> bundled default (parapetai_agent.policy.default_policies)
    entities_path=None,            # None -> bundled entities.json, or none if policy_dir has no entities.json
    agent_id=None,                 # None -> PARAPETAI_AGENT_ID env, else Agent::"anonymous"
    tenant="default",
    control_plane_url=None,        # None -> PARAPETAI_CONTROL_PLANE_URL env; both this + agent_secret opt into a real control plane
    agent_secret=None,             # None -> PARAPETAI_AGENT_SECRET env
    pep_key_path=None,             # None -> PARAPETAI_PEP_KEY_PATH env, else ~/.parapetai/pep_ed25519.key
    persist_policy_dir=None,       # None -> a control-plane-pulled bundle stays in memory only
    persist_pep_key=True,          # False -> this PEP's Ed25519 identity is never written to disk either
    local_log_dir=None,            # None -> no local rotating audit-log file (console output happens regardless)
)
```

The **minimal call** is `GovernedAgent(client=..., name=..., instructions=...)`
-- three required kwargs, real Cedar enforcement (base permits) from the
moment it's constructed, zero setup. Everything else only needs setting
once you have a specific requirement (your own policies, a real control
plane, disk-free serverless, a local audit trail).

**Fully disk-free construction** (serverless/read-only container, all
three closed at once):

```python
GovernedAgent(
    client=..., name="...", instructions="...",
    control_plane_url=..., agent_secret=...,   # or env vars
    persist_pep_key=False,
    # policy_dir/persist_policy_dir/local_log_dir already default to disk-free
)
```

Worked examples: `maf_sample_01/` (minimal, bundled default policy),
`maf_webapp/web_app.py` (explicit `policy_dir=POLICIES` + real
`persist_policy_dir` -- a deployed app *with* a mounted volume, choosing
to use it).

## 3. Invoking it — streaming vs. non-streaming

`GovernedAgent` does not override `run()` -- confirmed by reading the
source, there is no `def run` anywhere in `parapetai_agent/maf.py`. It is
the exact, unmodified `agent_framework.Agent.run()`, same signature,
regardless of how the agent was constructed or whether any identity is
set:

```python
# non-streaming -- awaits the complete response
result = await agent.run(query)
print(result.text)

# streaming -- an async iterable of AgentResponseUpdate, NOT awaited
async for chunk in agent.run(query, stream=True):
    if chunk.text:
        print(chunk.text, end="", flush=True)
```

Cedar evaluation is not a separate method call. It happens
transparently, as a side effect of `run()`, because the two governance
middleware objects were spliced into `middleware=[...]` back at
`__init__` time -- there is nothing to remember at the call site, and
nothing that differs between the streaming and non-streaming shapes
above; both go through the identical middleware chain. `maf_sample_01/`
demonstrates both back to back on the same agent.

## 4. Setting identity — five ways, one underlying mechanism

Identity is **fully decoupled** from invocation: it's set separately,
around a `run()` call (or persisted under a key for later calls), and
`run()` itself never changes shape depending on whether anything set it.
Skip identity entirely and Cedar evaluates against empty
`identity_claims`/`identity_roles` -- for a policy that checks identity,
that's a **deny** (Cedar's own default-deny working as designed, not a
bypass); for a policy that doesn't check identity, it's a no-op.

### 4a. `governed_identity()` -- the one call for a single `run()`, any source

```python
from parapetai_agent import governed_identity

# already-parsed claims/roles (e.g. from your own session store, an ID token you already decoded)
with governed_identity(claims={"oid": "..."}, roles=["OrderViewer"]):
    result = await agent.run(query)

# a raw bearer JWT (e.g. an inbound Authorization header)
with governed_identity(token=jwt):
    result = await agent.run(query)

# an azure-identity credential (e.g. the SAME one passed to FoundryChatClient)
credential = AzureCliCredential()
async with GovernedAgent(client=FoundryChatClient(credential=credential), ...) as agent:
    with governed_identity(credential=credential):
        result = await agent.run(query)
```

Internally dispatches to `current_identity()` / `identity_from_bearer_token()`
/ `identity_from_azure_credential()` based on which single kwarg you
pass -- you never have to pick which of those three matches your data's
shape. Pass exactly one source; zero or more than one both raise
`ValueError` immediately (fail loud on a misuse, rather than silently
picking one or silently asserting nothing). Worked example:
`maf_sample_01/` (`credential=`). `parapetai-agent/tests/test_maf.py::TestTokenDrivenIdentity`
demonstrates the underlying mechanism `token=` dispatches to
(`identity_from_bearer_token()`, called directly there) through a real
`Agent.run()`.

**End-user vs. agent identity, from the SAME token** (relevant to
`token=`/`credential=` specifically): a decoded JWT is checked for BOTH
end-user claims (`oid`/`sub`/`tid`/`preferred_username`/`upn`/`email` +
`roles`/`groups` -> Cedar's `identity_claims`/`identity_roles` context)
AND a delegation signal (RFC 8693's `act` claim, or a fallback to a bare
`azp`/`appid` string -> **overrides the Cedar principal itself**,
replacing whatever `agent_id=` the agent was constructed with). This is
automatic and claim-driven, not something you choose via a parameter --
know which claims your real tokens carry before assuming which behavior
you'll get. A plain `az login` token commonly carries `appid`/`azp`
(identifying Azure CLI as the client) even with no real delegation chain
involved -- worth decoding a real token to confirm before relying on
this.

### 4b. `identity_store` -- persisted under a key, for several invokers in one process

```python
from parapetai_agent import IdentityKeyKind, set_identity, use_identity

set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])   # once, e.g. after a lookup step
...
with use_identity("alice"):
    result = await agent.run(query)
```

The CLI/batch-workflow answer: no web request to extract identity from,
just several distinct invokers (users, threads, jobs) sharing one
process and one `GovernedAgent`, each needing their OWN identity kept
correctly separate. `IdentityKeyKind` (`SESSION`/`THREAD`/`CUSTOM`)
namespaces keys so two unrelated concepts sharing a string value (a
`session_id` and an unrelated `thread_id`) don't collide.
`configure_identity_store()` swaps the default in-memory backend for a
real shared one (Redis, a database) once this runs as more than one
replica -- required, not optional polish, once that's true (an
in-memory store is invisible across replicas, silently reverting to
anonymous rather than erroring). Worked example: `maf_cli/run_example.py`
(three distinct invokers, real Cedar allow/deny per identity).

### 4c. `IdentityMiddleware` -- automatic, per HTTP request

```python
from parapetai_agent import IdentityMiddleware

def _extract_identity(request: Request) -> tuple[Mapping, Sequence] | None:
    session = sessions.get(request.cookies.get("session_id"))
    if session is None or not session["identity_claims"]:
        return None
    return session["identity_claims"], session["identity_roles"]

app.add_middleware(IdentityMiddleware, extractor=_extract_identity)
```

Wired **once**, not per route. Enters `current_identity()` around every
inbound request automatically, reading it via your own `extractor=`
callable -- the web-server answer to "many concurrent end users, one
shared agent, no cross-talk between sessions." No `with governed_identity(...):`
needed inside any individual route handler; the middleware already did
it before the handler runs. `identity_from_claims()` (a plain function,
not a context manager) is the usual partner for this -- decode an
already-parsed ID token result once at login time and store the result
for the extractor to find later, exactly like `identity_from_claims()`'s
own docstring describes. Worked example: `maf_webapp/web_app.py`
(`_extract_identity()` + `IdentityMiddleware`, real Entra ID login).

### 4d. Explicit per-call override -- bypasses ambient entirely

```python
result = await agent.run(
    query,
    function_invocation_kwargs={"identity_claims": {...}, "identity_roles": [...]},
)
```

Wins over anything ambient for that ONE call (a full replace, not a
merge) -- the escape hatch for a caller that already has per-call
identity in hand and doesn't want any of the above.

### 4e. No identity at all

There is no dedicated "no identity" function or parameter -- omission
*is* the signal. Just call `agent.run(query)` directly, unwrapped. A
policy that doesn't check identity behaves identically either way; a
policy that does denies, per Cedar's own default-deny.

## 5. The combinations

Every row below is process-shape × identity-method, each demonstrated by
a real, runnable example -- pick the row closest to your own shape and
copy its wiring.

| Process shape | Identity method | Streaming? | Worked example |
|---|---|---|---|
| One-shot CLI script | none | both | `maf_sample_01/` |
| One-shot CLI script | `governed_identity(credential=...)` | both | `maf_sample_01/` |
| One-shot CLI script | `governed_identity(token=...)` (mechanism proven via `identity_from_bearer_token()`) | n/a | `parapetai-agent/tests/test_maf.py::TestTokenDrivenIdentity` |
| CLI, several invokers, one process | `identity_store` (`set_identity`/`use_identity`) | n/a | `maf_cli/run_example.py` |
| Long-running web server, many concurrent users | `IdentityMiddleware` (automatic per-request) | streaming (SSE) | `maf_webapp/web_app.py` |
| Function tools, Cedar-governed `tool_call` | none (governance only) | n/a | `maf_sample_02/` |
| Multi-turn conversation (`AgentSession`) | none | n/a | `maf_sample_03/` |
| Context provider / cross-turn memory | none | n/a | `maf_sample_04/` |
| Human-in-the-loop tool approval + Cedar | none | non-streaming only | `maf_sample_05/` |
| Framework-native middleware layered with governance | none | n/a | `maf_sample_06/` |
| Structured output (`response_format=`) | none | both | `maf_sample_07/` |
| Real control-plane-provisioned agent, disk persistence | any of the above | any | `maf_webapp/web_app.py` (`persist_policy_dir=CONTROL_PLANE_POLICY_CACHE`) |
| Fully disk-free (serverless/read-only container) | any of the above | any | see §2's "fully disk-free construction" |

Nothing in the identity column changes what invocation looks like
(§3); nothing in the invocation column changes what identity setup
looks like (§4) -- they're independent axes, which is why the table
above is a cross-product, not a fixed set of special cases.

## 6. Observability -- local file, and/or shipped to a control plane

**Automatic by default.** Once `control_plane_url`/`agent_secret` (or
their `PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET` env fallbacks)
resolve, `GovernedAgent`/`build_middleware()` calls `configure_otel()`
FOR you -- real spans/LogRecords (`parapetai.model_call`/`parapetai.tool_call`)
ship to that control plane's OTLP receiver with zero extra code. The
only knob exposed at that level is `otel_log_mode`:

```python
GovernedAgent(client=..., name=..., instructions=..., otel_log_mode="streaming")  # default: "buffered"
```

`"buffered"` (default) batches spans/LogRecords up to `batch_max_size`
(512) records OR `batch_schedule_delay_s` (120s -- two minutes) seconds,
whichever comes first; `"streaming"` sends each one immediately instead
-- lower control-plane request volume vs. lower latency. No control
plane configured: nothing is auto-wired (nowhere to ship to).

**Customizing beyond that** -- a custom `service_name`, Azure Monitor
export, disabled console output, batch tuning -- call `configure_otel()`
yourself, explicitly, BEFORE constructing `GovernedAgent`/calling
`build_middleware()`:

```python
from parapetai_agent import configure_otel, flush_otel

configure_otel(
    service_name="my-agent",
    otlp_endpoint=os.environ.get("PARAPETAI_CONTROL_PLANE_URL"),   # ships to parapetai_control/otlp.py's real OTLP receiver
    otlp_headers={"Authorization": f"Bearer {agent_secret}"},
    azure_monitor_connection_string=os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"),  # optional, independent
    log_mode="buffered",              # default -- "streaming" sends each span/log immediately instead
    batch_max_size=512,               # buffered mode only
    batch_schedule_delay_s=120,       # buffered mode only -- 2 minutes, deliberately longer than OTel's own 5s default
)
```

Whichever `configure_otel()` call happens FIRST wins (OTel's own
`TracerProvider`/`LoggerProvider` registration is process-wide and
set-once) -- calling it explicitly, earlier, makes the auto-wiring
detect OTel is already configured and step aside entirely, never
silently overriding your setup. See `maf_webapp/web_app.py`'s
`lifespan()` for exactly this (its own `configure_otel()` call, for
Azure Monitor export and a custom `service_name`, runs before
`build_middleware()`'s priming call).

Either way, independent of `GovernedAgent(..., local_log_dir=...)`,
which is a local, always-immediate (`logging.Handler.emit()` flushes
per record, nothing to lose on exit) rotating JSON-lines file -- have
either, both, or neither. `configure_otel()` registers an `atexit` hook
that flushes any buffered-but-unsent telemetry on normal process exit; a
long-running server should ALSO call `flush_otel()` explicitly from its
own shutdown sequence (`atexit` doesn't fire on SIGTERM) -- see
`maf_webapp/web_app.py`'s `lifespan()` `finally:` block for exactly
that.

## 7. Serverless / disk-free deployment checklist

| Concern | Default | Disk-free setting |
|---|---|---|
| Cedar policy + entities | bundled package data (read-only, no volume needed) | `policy_dir=None` (the default -- nothing to change) |
| Control-plane-pulled bundle | memory only unless told otherwise | `persist_policy_dir=None` (the default -- nothing to change) |
| PEP's Ed25519 identity | `~/.parapetai/pep_ed25519.key` | `persist_pep_key=False` |
| Local decision-audit file | off | `local_log_dir=None` (the default -- nothing to change) |
| `agent_id` | just a string, never disk-backed either way | n/a |

Three of five are already disk-free by default; the only one requiring
an explicit opt-out is `persist_pep_key` (defaults `True`, trading a
stable cross-restart identity for the convenience of a real key file --
see `parapetai_agent.pep_identity.generate_ephemeral_keypair()`'s own
docstring for the tradeoff of turning it off).

## 8. Reference implementations

| Directory | Demonstrates |
|---|---|
| [`maf_sample_01/`](maf_sample_01/README.md) | Minimal wiring, streaming + non-streaming, `governed_identity(credential=...)` |
| [`maf_sample_02/`](maf_sample_02/README.md) | Function tools as a real Cedar `tool_call` |
| [`maf_sample_03/`](maf_sample_03/README.md) | Multi-turn conversation via `AgentSession` |
| [`maf_sample_04/`](maf_sample_04/README.md) | Context provider / cross-turn memory |
| [`maf_sample_05/`](maf_sample_05/README.md) | Human-in-the-loop tool approval composed with Cedar |
| [`maf_sample_06/`](maf_sample_06/README.md) | Framework-native middleware layered after governance middleware |
| [`maf_sample_07/`](maf_sample_07/README.md) | Structured output, plain OpenAI routing |
| [`maf_webapp/`] (private control-plane repo) | Long-running web server, `IdentityMiddleware`, MCP tools, real Entra ID login, SSE streaming, OTel export |
| [`maf_cli/`](maf_cli/README.md) | Several distinct invokers/identities in one process via `identity_store` |

See [`docs/maf-integration-pattern.md`](../docs/maf-integration-pattern.md)
for the reasoning behind each construction default (why the bundled
policy lives in `parapetai-agent` not `parapetai-agent`, why `persist_policy_dir`
defaults to memory-only, why `GovernedAgent` doesn't monkeypatch
`agent_framework.Agent`, ...) -- this document is the map, that one is
the terrain.
