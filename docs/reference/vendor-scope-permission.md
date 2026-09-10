# Automatic vendor/resource/permission detection (`observation`)

`parapetai_agent.observation` — since **0.11.0**. Automatically detects
which vendor, product, resource, and permission a tool call really touches,
by watching the real network traffic it makes — **no `@declare_vendor_call`
required**. This is the successor to hand-declared
[vendor/CRUD metadata](vendor-calls.md): instead of every agent author
having to state a tool's baseline vendor/CRUD shape up front, the SDK
captures the raw shape of a call and ships it to the control plane, which
classifies it and presents a match for a human to accept.

This module only ever **captures** — it never classifies. Turning a raw
observation into a vendor/product/resource/permission match, presenting it
for review, and generating the Cedar grant once accepted are all control
plane jobs; see [Vendor/product/resource/permission triage](#seeing-it-in-the-control-plane)
below.

## Three capture paths, one shape, all fully automatic

| Where | How | Your code |
|---|---|---|
| In-process, non-MCP tool (MAF, ADK, LangGraph, Governor) | Rides on [corroboration](corroboration.md)'s real outbound-call spans; tagged by an `ObservationSpanProcessor` | **None** — auto-wired by `build_middleware()`/`build_plugin()`/`Governor.from_control_plane()` whenever a control plane is configured |
| In-process, tool is itself an MCP client (e.g. `agent_framework.MCPTool`, `google.adk`'s MCP tool support) | `mcp.client.session.ClientSession.call_tool`/`initialize` are patched directly — `name`/`arguments` arrive as plain Python values, no span involved | **None** — same auto-wiring as above |
| Gateway (`parapetai-gateway`, MCP path) | The inbound `tools/call` request the gateway is already proxying **is** the observation — `MCPParser` already extracts `tool_name`/`tool_args` with certainty | **None** — automatic once the gateway has a control plane configured |

All three emit the identical wire shape (`parapetai.observed.*` span
attributes over the same OTLP `/v1/traces` pipe every decision already
uses), so the control plane's ingestion and triage UI don't care which one
produced a given bucket. None of the three requires `configure_otel()`,
`enable_http_corroboration()`, `enable_observation_capture()`, or
`enable_mcp_observation()` to be called by hand — see below for what's
actually happening and how to opt out.

### Why a tool that is itself an MCP client needs a separate path

The first row above — a tool making its own outbound HTTP/gRPC/DB call —
is what `corroboration`/`ObservationSpanProcessor` were built for: the
call happens synchronously inside the framework's own `parapetai.tool_call`
span, so tagging it just means reading that span's attributes.

That assumption breaks for a tool that is *itself* an MCP client (e.g.
`agent_framework.MCPTool` and its `MCPStdioTool`/`MCPStreamableHTTPTool`/
`MCPWebsocketTool` subclasses, or `google.adk`'s own MCP tool support) —
both were confirmed, by reading their source, to dispatch the real network
call through a **persistent background task** that owns the MCP session
for the tool's whole lifetime, not synchronously inside whichever
`parapetai.tool_call` span happened to trigger a given request. The call
is real and does happen, but it runs detached from that span — no parent
link, and no `current_framework()` for `ObservationSpanProcessor` to gate
on, so span-based tagging structurally cannot see it.

The fix sidesteps the problem instead of solving it: `enable_mcp_observation()`
patches `mcp.client.session.ClientSession.call_tool`/`initialize` directly
— the one layer, confirmed against both `agent_framework`'s and
`google-adk`'s own MCP tool implementations, that every in-process MCP
client integration this SDK has verified funnels a `tools/call` through,
regardless of what background-task architecture sits above it. `name`/
`arguments` arrive as plain Python values here, before any JSON-RPC
serialization even happens, so — unlike the gateway's own `MCPParser`,
which has to parse wire bytes because it never sees the caller's native
call — no body-parsing is needed. `initialize()`'s response also carries
the remote server's own declared name (and website, if given), cached and
used as `target`/`destination` for every later `call_tool` on that same
session — the same "the routing name is often already vendor-indicative"
reasoning §10.11 gives for the gateway's own `/mcp/{target}`.

Patching the class, not each higher-level tool wrapper, means this covers
every integration built on the standard `mcp` package from one place —
you don't need to know or care which framework wraps it.

## In-process: MAF, ADK, LangGraph, Governor

Nothing to call. The moment a control plane is configured —
`control_plane_url`/`agent_secret` (arguments, or the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET` env vars) —
`build_middleware()`/`build_plugin()`/`Governor.from_control_plane()` wire
up all four of `configure_otel()`, `enable_http_corroboration()`,
`enable_observation_capture(agent_id)`, and `enable_mcp_observation(agent_id)`
for you, in that order, the same way they already auto-configured OTel
before this existed. This is a deliberate design choice, not just a
convenience: the whole premise of automatic detection (§10.0) is that it
requires no manual step from the agent author, and the mechanism is
inherently self-limiting (see "Honoring the collection budget" below) —
there's no unbounded standing cost that would argue for defaulting it off.

```python
from parapetai_agent.maf import build_middleware  # or adk.build_plugin,
                                                    # langgraph.build_middleware,
                                                    # Governor.from_control_plane

mw = build_middleware(
    control_plane_url="https://your-control-plane",
    agent_secret="...",
    agent_id="agent-42",
)
# Detection is already running -- nothing else to call.
```

`agent_id` is the identity `enable_observation_capture()` gets called
with, and it's exactly the identity your `control_plane_url`/`agent_secret`
pair authenticates as — the control plane derives the `agent_id` a bucket
gets stored under from the bearer secret on the OTLP POST, never from a
span attribute, so this always lines up correctly without you having to
keep two values in sync yourself.

Each adapter also tags every `parapetai.tool_call` span it opens with its
own framework name (`maf`/`adk`/`langgraph`/`governor`), which flows onto
any observation captured inside that span.

### Opting out

Pass `observation_capture=False` (or set `PARAPETAI_OBSERVATION_CAPTURE=false`,
process-wide) to disable this specific auto-wiring — OTel export for
decision audit is unaffected either way, since that's independent of
detection:

```python
build_middleware(control_plane_url=..., agent_secret=..., observation_capture=False)
```

### Honoring the collection budget

The control plane caps how many samples it wants per distinct call shape,
then tells every PEP to stop enriching that shape once enough exist
(`observation_collection.saturated_buckets` on the bundle-poll response) —
this bounds both the OTLP traffic and control-plane storage this feature
costs. This is wired automatically too: the `CollectionBudget`
`enable_observation_capture()` returns is fed as the same background
bundle-poll thread's `on_bundle_meta` callback, so a server-issued
saturation (or resume) instruction takes effect on the very next regular
policy-refresh cycle, with no separate polling channel and nothing extra
for you to wire.

If you're calling `parapetai_agent.observation`/`corroboration` and
`control_plane.run_bundle_poller()`/`bootstrap_engine()` directly instead
of going through one of the four entry points above (e.g. a custom
integration), `bootstrap_engine()`'s own `on_bundle_meta` parameter now
reaches every poll cycle of its background thread, not just its one-shot
synchronous first fetch — wire it the same way:

```python
from parapetai_agent.observation import enable_observation_capture, enable_mcp_observation
from parapetai_agent.control_plane import bootstrap_engine

budget = enable_observation_capture("agent-42")  # after configure_otel()
enable_mcp_observation("agent-42", budget)  # if the agent has any MCP-client tools
boot = bootstrap_engine(
    control_plane_url, agent_secret, policy_dir=...,
    on_bundle_meta=budget.update_from_bundle_meta,
)
```

`enable_mcp_observation()` is a no-op (returns `None`, does not raise) if
the `mcp` package isn't installed — safe to call unconditionally even if
your agent has no MCP tools at all.

## Gateway: fully automatic on the MCP path

No code, no extra call. Once a gateway deployment has a control plane
configured — the same `PARAPETAI_AGENT_ID`/`PARAPETAI_AGENT_SECRET`/
`PARAPETAI_CONTROL_PLANE_URL` a gateway already needs for policy bundles and
decision audit — every `tools/call` request it proxies that is not blocked
gets observed automatically, and the collection budget is honored from the
very same bundle-poll response that already refreshes its Cedar policy.

Set `PARAPETAI_OBSERVATION_CAPTURE=false` to opt out, same variable as the
in-process SDK. The gateway's `agent_id` for this purpose is always
`PARAPETAI_AGENT_ID` (the gateway's own registered fleet identity — the
same reasoning as above: it's what actually authenticates the OTLP
export), never the unauthenticated per-request `/a/{agent_id}` path claim
a caller presents.

## Seeing it in the control plane

Once a call has been observed, the control plane classifies it (vendor →
product → resource → permission, in that order, each independently
optional) and surfaces a ranked match for a human to accept, create a new
tenant-scoped catalog entry from, fall back to a bare tool-name rule for, or
dismiss — an operator never has to hand-author a match from nothing.
Accepting one both grants it (so future matching calls are enforced by
Cedar, same as any other grant) and schedules a periodic re-check so a
later drift from the accepted shape gets flagged, not silently ignored.

Find it under an agent's own page — **Vendor / product / resource /
permission triage** — in both the legacy console (`/agents/{agent_id}/vsp-triage`)
and `control-plane-v2` (`/a/{account_id}/agents/{agent_id}`).

## See also

- [Vendor/CRUD metadata](vendor-calls.md) — the older, hand-declared
  signal this module's control-plane-side classification supersedes as the
  primary path; `@declare_vendor_call` still works as an optional override
  signal the inference engine weighs highest when present.
- [Corroboration](corroboration.md) — the real-span capture this module's
  in-process half rides on.
- [Observability](../OBSERVABILITY.md) — `configure_otel()` and the OTLP
  export pipeline both paths use.
