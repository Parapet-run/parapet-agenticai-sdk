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

## Two capture paths, one shape

| Where | How | Your code |
|---|---|---|
| In-process (MAF, ADK, LangGraph, Governor) | Rides on [corroboration](corroboration.md)'s real outbound-call spans; tagged by an `ObservationSpanProcessor` | One extra call — see below |
| Gateway (`parapetai-gateway`, MCP path) | The inbound `tools/call` request the gateway is already proxying **is** the observation — `MCPParser` already extracts `tool_name`/`tool_args` with certainty | **None** — automatic once the gateway has a control plane configured |

Both paths emit the identical wire shape (`parapetai.observed.*` span
attributes over the same OTLP `/v1/traces` pipe every decision already
uses), so the control plane's ingestion and triage UI don't care which one
produced a given bucket.

## In-process: MAF, ADK, LangGraph, Governor

Requires [corroboration](corroboration.md) to already be capturing real
outbound spans — this module only tags a subset of those, it doesn't
instrument anything itself:

```python
from parapetai_agent import configure_otel
from parapetai_agent.corroboration import enable_http_corroboration
from parapetai_agent.observation import enable_observation_capture

configure_otel(otlp_endpoint="...")     # 1. real spans have somewhere to go
enable_http_corroboration()             # 2. real spans get created at all
enable_observation_capture("agent-42")  # 3. tag the ones worth classifying
```

Same ordering rule as `enable_http_corroboration()` (see
[corroboration.md's own note](corroboration.md#ordering-call-this-after-configure_otel-never-before)):
call this *after* `configure_otel()`, or every span it ever tags goes to a
provider nothing exports from. `enable_observation_capture()` registers
itself on whichever `TracerProvider` is globally current at call time, and
does not re-check later.

`agent_id` must be the same identity your `control_plane_url`/`agent_secret`
pair authenticates as — the control plane derives the `agent_id` a bucket
gets stored under from the bearer secret on the OTLP POST, never from a
span attribute, so a mismatched value here means your local saturation
checks can never agree with what the server computes.

No further wiring is needed for MAF/ADK/LangGraph/Governor themselves —
each adapter already tags every `parapetai.tool_call` span it opens with
its own framework name (`maf`/`adk`/`langgraph`/`governor`), which flows
onto any observation captured inside that span automatically.

### Honoring the collection budget

The control plane caps how many samples it wants per distinct call shape,
then tells every PEP to stop enriching that shape once enough exist
(`observation_collection.saturated_buckets` on the bundle-poll response) —
this bounds both the OTLP traffic and control-plane storage this feature
costs. `enable_observation_capture()` returns a `CollectionBudget` you must
keep updated from that response yourself if you are not using
`run_bundle_poller()`/`bootstrap_engine()`'s bundled poller with its own
`on_bundle_meta` hook:

```python
from parapetai_agent.control_plane import run_bundle_poller

budget = enable_observation_capture("agent-42")
run_bundle_poller(
    control_plane_url, agent_secret, policy_dir,
    on_bundle_meta=budget.update_from_bundle_meta,
)
```

**Known gap, not yet closed**: `bootstrap_engine()` (and the
`build_middleware()`/`build_plugin()`/`Governor.from_control_plane()`
convenience wrappers built on it) do not yet expose an `on_bundle_meta`
passthrough for their own background poller thread — only the framework
adapters' *synchronous* first fetch reads `vendor_scoped_resources` this
way today. Until that passthrough is added, an app using one of those
high-level entry points that also wants live saturation updates needs to
run its own `run_bundle_poller()` call (as above) alongside the one the
convenience wrapper already starts, rather than relying on a single poller
for both. Collection still starts and works without this — a bucket simply
never receives its stop instruction, so it keeps enriching past the
server's intended cap until the process restarts against a fresh
`CollectionBudget`. Filed here rather than silently worked around.

## Gateway: fully automatic on the MCP path

No code, no extra call. Once a gateway deployment has a control plane
configured — the same `PARAPETAI_AGENT_ID`/`PARAPETAI_AGENT_SECRET`/
`PARAPETAI_CONTROL_PLANE_URL` a gateway already needs for policy bundles and
decision audit — every `tools/call` request it proxies that is not blocked
gets observed automatically, and the collection budget is honored from the
very same bundle-poll response that already refreshes its Cedar policy.

There is nothing to turn on or off separately; the gateway's `agent_id` for
this purpose is always `PARAPETAI_AGENT_ID` (the gateway's own registered
fleet identity — the same reasoning as above: it's what actually
authenticates the OTLP export), never the unauthenticated per-request
`/a/{agent_id}` path claim a caller presents.

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
