# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.13.0]

### Added
- **A third automatic-detection capture path, for a tool that is itself
  an MCP client.** `enable_mcp_observation()` (`parapetai_agent.observation`),
  auto-wired alongside the other three (`configure_otel`/
  `enable_http_corroboration`/`enable_observation_capture`) by
  `build_middleware()`/`build_plugin()`/`Governor.from_control_plane()`.
  Corroboration's span-based capture assumes a tool's real network call
  happens synchronously inside its own `parapetai.tool_call` span — that
  assumption is false for `agent_framework.MCPTool` (and its Stdio/
  StreamableHTTP/Websocket subclasses) and `google.adk`'s own MCP tool
  support: both were confirmed, by reading their source, to dispatch a
  `tools/call` through a persistent background task that owns the MCP
  session for the tool's whole lifetime, detached from whatever span
  triggered any given call. Rather than fix that upstream, this patches
  `mcp.client.session.ClientSession.call_tool`/`initialize` directly —
  the one layer both integrations were confirmed to funnel every call
  through regardless of framework, receiving `name`/`arguments` as plain
  Python values (no JSON-RPC body parsing needed, unlike the gateway's
  own `MCPParser`). `initialize()`'s response is also used: the remote
  server's own declared name/website get cached per-session and used as
  `target`/`destination` for every later `call_tool`. Shares the same
  `CollectionBudget` and `PARAPETAI_OBSERVATION_CAPTURE` opt-out as the
  other three. A no-op (never raises) when the `mcp` package isn't
  installed.

## [0.12.0]

### Changed
- **Automatic vendor/resource/permission detection (0.11.0) is now ON BY
  DEFAULT**, not a three-call manual opt-in. `build_middleware()`
  (MAF/LangGraph), `build_plugin()` (ADK), and `Governor.from_control_plane()`
  now call `configure_otel()`, `corroboration.enable_http_corroboration()`,
  and `observation.enable_observation_capture()` for you, in that order,
  the moment a control plane is configured — matching the feature's own
  design premise (auth-integrations.md §10.0: no manual step required)
  and the fact that it's inherently self-limiting via the control plane's
  own collection-budget signal, not an unbounded standing cost. New
  `observation_capture: bool | None = None` parameter (env fallback
  `PARAPETAI_OBSERVATION_CAPTURE`, default `true`) on all four entry
  points opts out. `Governor.from_control_plane()` previously had **no**
  `configure_otel()` auto-wiring at all (a real, separate pre-existing
  gap, unlike the other three) — closed here too, since automatic
  detection needs it working to go anywhere.
- **Gateway (`parapetai-gateway`)**: Phase B's MCP-path observation
  capture (0.11.0) is likewise unconditional-by-default and now respects
  the same `PARAPETAI_OBSERVATION_CAPTURE` env var (new `Settings.
  observation_capture` field, default `true`) to opt out fleet-wide.
- **`control_plane.bootstrap_engine()`'s `on_bundle_meta` now reaches
  every cycle of its own background poller thread**, not just its
  one-shot synchronous first fetch — closes the gap 0.11.0's own docs
  flagged as known-but-unfixed. A caller wiring
  `CollectionBudget.update_from_bundle_meta` through one of the four
  high-level entry points above no longer needs a second, manual
  `run_bundle_poller()` call to get live saturation updates.

### Fixed
- **`observation.ObservationSpanProcessor` no longer misclassifies this
  SDK's own control-plane traffic as an observed vendor call.** Real bug,
  found making detection default-on: `enable_http_corroboration()`
  instruments httpx process-wide, with no awareness of what called it —
  without a fix, every bundle-poll/heartbeat/key-registration request
  this SDK makes to its OWN control plane would get corroborated and
  potentially tagged as an "observed call" too. Two independent fixes,
  not one: (1) `ObservationSpanProcessor.on_start()` now only tags a span
  started while `current_framework()` is set (i.e. genuinely inside some
  framework's own tool_call scope) — this SDK's own infrastructure calls
  never run inside one; (2) `control_plane.py`'s own outbound HTTP calls
  (bundle fetch, heartbeat, key registration, review submit/collect) now
  wrap themselves in a new `_suppressed_instrumentation()` helper, using
  `opentelemetry.context`'s own suppress-instrumentation key directly, so
  corroboration's instrumentor never creates a span for them at all —
  belt and suspenders, since (1) alone only stops misclassification, not
  the extra OTLP export traffic/cost (1) alone would still generate for
  every recurring poll cycle, forever, for every control-plane-configured
  process.

## [0.11.0]

### Added
- **Automatic vendor/resource/permission detection** (`parapetai_agent.observation`),
  replacing hand-declared `@declare_vendor_call` as the *primary*
  classification path (the decorator still works, now as an optional,
  highest-weighted override signal). Detection happens by watching a
  tool's real network traffic — no per-tool declaration burden on the
  agent author — and the control plane classifies what it observes into
  a vendor/product/resource/permission match for a human to accept. See
  [the new reference doc](docs/reference/vendor-scope-permission.md) for
  the full picture, including the two capture paths below.
- **In-process capture** (MAF, ADK, LangGraph, Governor): a new
  `enable_observation_capture(agent_id)` call tags a subset of the spans
  [`corroboration`](docs/reference/corroboration.md) already produces.
  Each framework adapter now also tags its own `parapetai.tool_call` span
  with its framework name (`maf`/`adk`/`langgraph`/`governor`), so a
  captured observation carries which integration produced it. New
  `corroboration-db` extra (`psycopg2`/`pymongo`/`redis`/`sqlalchemy`
  instrumentors) extends corroboration's existing HTTP/gRPC-only coverage
  to a tool's own datastore calls, as a separate opt-in
  (`enable_db_corroboration()`) that doesn't change `enable_http_corroboration()`'s
  existing behavior.
- **Gateway capture, fully automatic**: `parapetai-gateway` now observes
  every proxied `tools/call` MCP request that isn't blocked, with zero
  configuration beyond what a control-plane-governed gateway deployment
  already needs (`PARAPETAI_AGENT_ID`/`PARAPETAI_AGENT_SECRET`/
  `PARAPETAI_CONTROL_PLANE_URL`). Unlike the in-process path, there is no
  outbound span to piggyback on — the gateway already parses the tool
  call off the wire with certainty, so it constructs the observation
  directly and exports it as a standalone span via the same OTLP pipe its
  decision audit already uses.
- **Server-controlled collection budget**: the control plane caps how
  many samples it wants per distinct call shape, then instructs every PEP
  (in-process or gateway) to stop enriching that shape via the existing
  bundle-poll response — bounding the OTLP traffic and control-plane
  storage this feature costs, with no separate polling channel.

## [0.10.0]

### Changed
- **`console` now defaults to silent, resolved from `PARAPETAI_CONSOLE_LOG`
  (default `false`), on `maf.build_middleware()`/`GovernedAgent`,
  `adk.build_plugin()`/`GovernedRunner`, and `langgraph.build_middleware()`.**
  Previously `console: bool = True` on all five, so a governed run printed
  a raw structlog/OTel decision stream to stdout unless every embedder
  remembered `console=False` -- exactly the boilerplate `examples/maf_webapp/`
  and every quickdemo template had to repeat. An explicit `console=True`/
  `False` at the call site still wins outright; only the *default* (an
  omitted kwarg) changed, from a literal `True` to `None`, which now
  resolves to the env var. **This is a behavior change for any embedder
  relying on the old default** -- pass `console=True` (or set
  `PARAPETAI_CONSOLE_LOG=true`) to keep the previous behavior.
- **`local_log_dir` now also falls back to `PARAPETAI_LOCAL_LOG_DIR`** on
  the same five entry points, same "explicit always wins, env var only
  fills an omitted kwarg" rule. Still `None`/off by default when neither
  is set -- no behavior change there, just one less thing an embedder has
  to thread through their own env-reading code by hand.
- Both resolved by one new shared function,
  `governance_runtime.resolve_local_output_settings()`, rather than each
  of `maf.py`/`adk.py`/`langgraph.py` hand-rolling its own env fallback (as
  `PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID`
  already independently do in each file) -- one implementation so the
  three framework integrations can't drift on what "unset" means for
  these two.

## [0.9.0]

### Added
- **`vendor_scoped_resources` exposed directly on `GovernedAgent` and
  `GovernedRunner`.** Both wrapper classes always forwarded every other
  `build_middleware()`/`build_plugin()` kwarg except this one -- a real
  gap: there was no way to turn on vendor-scoped Cedar resources for a
  `GovernedAgent`/`GovernedRunner` with no control plane configured, only
  for a caller using `build_middleware()`/`build_plugin()` directly. Now a
  plain constructor kwarg on both, passed straight through.
- **Cumulative cost/token tracking for LangGraph and `Governor`**
  (previously MAF/ADK only). `ParapetAgentMiddleware` gained
  `before_agent`/`abefore_agent`/`after_agent`/`aafter_agent` to bracket
  the TRACE scope (no OTel `SpanContext` exists on this adapter yet, so
  ids are generated and threaded through contextvars instead of derived
  from one); `wrap_model_call`/`awrap_model_call` populate it the same way
  `maf.py`'s own COST-TRACK logic does. `Governor` gained a new `trace()`
  context manager (the explicit trace boundary it needs, having no
  framework loop of its own to hook a "run started" callback into) and
  `check_output(..., prompt_tokens=, completion_tokens=)` -- `Governor`
  never sees the model's own response object, so usage has to be reported
  rather than learned automatically the way MAF/ADK/LangGraph do it.
  `policy/cost_tracker.py` gained `new_trace_id()`/`new_span_id()`, the
  shared id-generation helpers both new call sites use. See
  `docs/reference/cost-tracking.md`.
- **Vendor/CRUD metadata and `vendor_scoped_resources` wired into
  `Governor`.** `authorize_tool()` gained `func=`/`metadata=` (the same
  two declaration paths -- decorator vs. framework-native metadata dict --
  ADK/LangGraph already check, in the same order); `Governor.tool()`
  passes `func=f` automatically so the decorator path needs no extra
  wiring at the call site. `from_policy_dir()` gained
  `vendor_scoped_resources` (`from_control_plane()` instead always
  resolves it from the bundle, same priority rule the other three
  integrations use -- there's no meaningful override there). Reaches the
  exact same `context.vendor_system`/`crud_action` fields and Cedar
  `resource` construction MAF/ADK/LangGraph produce, so a control-plane
  connector-catalog match needs no special case for "this call came
  through `Governor`." See `docs/reference/vendor-calls.md`.

### Testing
- **`tests/test_governance_surface_parity.py`** -- a mechanical,
  cross-integration check that catches the exact bug class the
  `vendor_scoped_resources` gap above was: derives the full set of opt-in
  `GovernanceHook` constructor flags directly from its own signature and
  asserts every integration surface (`Governor`, MAF, ADK, LangGraph)
  accepts each one, or is explicitly exempted with a documented reason.
  A second test in the same file asserts every such flag is mentioned
  somewhere under `docs/` at all -- the gap that shipped `vendor_calls.py`/
  `corroboration.py`/`cost_tracker.py` with zero documentation for months.
  See CLAUDE.md's "Working agreements" for the policy this backs.

## [0.8.0]

### Added
- **HTTP/gRPC corroboration, Tier 1** (auth-integrations.md §7):
  `parapetai_agent.corroboration.enable_http_corroboration()` turns on real
  OpenTelemetry auto-instrumentation for `httpx`/`requests`/`urllib3`/
  `aiohttp`/`grpc` (new `parapetai-agent[corroboration]` extra), so a real
  network call a tool makes during its own execution -- however many
  layers of vendor SDK wrap it -- emits a span correctly correlated to
  that tool's own `parapetai.tool_call` span. Safe to call unconditionally:
  each candidate self-detects whether its target library is installed and
  no-ops rather than raising if not (`BaseInstrumentor`'s own dependency
  check), and calling it more than once is a no-op past the first time.
  `disable_http_corroboration()` reverses it. Deliberately capture-only in
  this release: comparing the resulting span against a tool's declared
  `crud_action` and any enforcement consequence is separate, not-yet-built
  work -- see the module's own docstring for why that's a control-plane
  analysis problem once the signal exists, not new SDK machinery. **Call
  this after, not before, `configure_otel()`** -- an instrumentor captures
  its tracer at `instrument()` time and does not observe a later
  `TracerProvider` change (documented prominently in the module's own
  docstring after hitting this live while building it).

## [0.7.0]

### Added
- **Declared vendor/CRUD metadata for tool calls.** `parapetai_agent.vendor_calls`:
  `VendorCallSpec`/`declare_vendor_call`/`resolve_vendor_call` (decorator-based,
  attaches to the underlying callable) and `resolve_vendor_call_from_metadata`
  (for ADK's `custom_metadata` / LangChain's `.metadata`). `Snapshot` carries
  the resolved facts as `vendor_system`/`vendor_operation`/`crud_action`,
  available to Cedar as `context.crud_action` etc. and never stripped by
  `content_free()`. Wired into MAF, ADK, and LangGraph.
- `GovernanceHook(..., vendor_scoped_resources=True)` (default `False`,
  threaded through `build_middleware()`/`build_plugin()` on all three
  frameworks): resolves a tool call's Cedar `resource` to
  `Resource::"<vendor_system>/<vendor_operation>"` instead of
  `Resource::"<provider>"` when vendor metadata was declared, and to the
  distinct `Resource::"undeclared"` when it wasn't -- so an unclassified
  tool never silently inherits whatever a provider-scoped `permit` already
  allows. Off by default: no existing bundle's resource-matching policies
  change behavior until this is explicitly turned on.
- `bootstrap_engine()`'s returned `Bootstrap` now carries
  `vendor_scoped_resources: bool`, resolved from the bundle response's own
  `vendor_scoped_resources` field when a control plane is configured --
  lets a control plane turn the flag above on per tenant without a code
  change on the PEP side. Resolved once, at bootstrap (process start);
  does not hot-reload mid-process on a later bundle poll (see
  `Bootstrap`'s own docstring for why). `poll_once()` gained a matching
  `on_bundle_meta` callback (the full bundle dict, not just `files`) that
  this is built on.

### Fixed
- `langgraph.py`'s `_tool_snapshot` read only `request.tool_call` (the raw
  `{name, args, id}` dict from the model's output), never `request.tool` --
  so a LangChain tool's own `.metadata` was completely unreachable by any
  Cedar decision. Now reads `request.tool` too (falls back to the raw dict
  when a tool isn't registered with the `ToolNode`, unchanged from before).
- `mode`'s default value was inconsistent across the control-plane client
  (`"enforce"` in `bootstrap_engine()`, `""` in `run_bundle_poller()`).
  Both now reference one `control_plane.DEFAULT_MODE` constant.

## [0.5.0]

### Added
- **LangGraph / LangChain integration.** `parapetai_agent.langgraph`
  (`langgraph` extra): `ParapetAgentMiddleware` / `build_middleware()`, a
  real `langchain.agents.middleware.AgentMiddleware` for
  `langchain.agents.create_agent(..., middleware=[...])`. Governs
  pre-model, tool-call, and post-model Cedar decisions with genuine
  construction-time blocking (verified live against
  `langchain==1.3.18`/`langgraph==1.2.11`), plus ambient identity via the
  same `governed_identity()` MAF/ADK already use. See
  `docs/frameworks/langgraph.md`. No `alter_transforms=`/ALTER support yet
  (tracked as a known gap in the module's own docstring).

## [0.4.2]

No functional changes. Release-pipeline verification (tag → build → PyPI
publish via Trusted Publishing).

## [0.4.1]

### Added
- Framework integration examples (`maf_sample_01` through `maf_sample_07`,
  `maf_cli`, `adk_sample_01`, `adk_webapp`, `ungoverned_vs_governed`) moved
  into this repo from the control-plane repo, plus a new
  `same_prompt_every_framework/` example: five frameworks, one Cedar rule,
  the same two prompts, side by side.

### Fixed
- `adk.py`'s `provider_for_request()` reported `"gemini"` unconditionally.
  It now reports the provider actually called.

## [0.4.0]

### Added
- **Resolve a held REVIEW call.** `authorize_tool()` now raises
  `GovernanceReviewRequired` with a ticket instead of only a plain deny;
  `Governor.wait_for_approval()` is an opt-in blocking helper. A grant is
  single-use and bound to one exact call via a content fingerprint —
  approving one call can't be replayed onto another. See
  `docs/adr/0009-approval-loop.md`.
- **The gateway joins the approval loop.** A held call's ticket rides the
  existing 403 refusal (`x-parapetai-review-id` header, or `error.data` for
  MCP clients that never see headers); the client re-presents it on retry,
  and the fingerprint is recomputed from the retried request body so an
  approved call can't be swapped for a different one.

### Fixed
- The release workflow's `environment: release` name didn't match the PyPI
  trusted-publisher config, failing publishes with `invalid-publisher`.

## [0.3.0]

### Added
- `Governor.from_control_plane()` — the framework-neutral embed path can now
  receive **control-plane-authored** policy, refreshed by a background poller.
  Previously only the Microsoft Agent Framework adapter could; every other
  framework was limited to `from_policy_dir()`, i.e. policy files the adopter
  maintained themselves. On an unreachable control plane it falls back to the
  last bundle on disk rather than failing to start; with nothing on disk it
  fails closed.
- **REVIEW decision outcome** — `@action("review")` on a Cedar `forbid` marks
  that deny as escalatable to a human, surfacing as `Decision.effect ==
  "review"`. `Decision.allowed` stays `False`, so any caller that only checks
  `allowed` blocks a held call exactly as it blocked a denied one. Requires
  unanimity across determining policies, so a hard `forbid` matching alongside
  a reviewable one keeps the deny hard. See `docs/adr/0008`.
- **Provider-agnostic SLM judge** (`litellm` backend, extra:
  `parapetai-agent[judge]`). The default `slm` backend builds an
  OpenAI/AzureOpenAI client and cannot reach a non-OpenAI-wire endpoint at all;
  this routes Anthropic, Bedrock, Vertex, Groq, Ollama and the rest through one
  code path rather than a vendor client per provider.

- **Google ADK integration** (`adk.py`: `GovernedRunner`, `ParapetPlugin`)
  behind its own `adk` extra, independent of `maf` -- `pip install
  parapetai-agent[adk]` works without ever importing `agent_framework`, and
  vice versa. Both source their shared runtime from `governance_runtime.py` /
  `scoped_data.py`, so a developer picks a framework without inheriting the
  other's dependencies.
- **The gateway PEP now ships from this repo** (`gateway/`), MIT-licensed and
  publishable, with a console script: `uvx parapetai-gateway`. It is the same
  enforcement role as this package in a different form factor — for apps that
  cannot embed the SDK, and for agents that aren't Python at all. `parapetai-mcp`
  and the conformance suite moved here too. The repo is now a uv workspace; the
  root remains the published `parapetai-agent` package, so `pip install
  parapetai-agent` is unchanged.

### Changed
- Control-plane bootstrap (identity registration, first fetch, disk-vs-memory
  policy load, heartbeat, poller thread) extracted to
  `control_plane.bootstrap_engine()` and shared by both embed paths. Two copies
  meant two sets of outage semantics.
- This repo is now the single source of truth for the Cedar engine. A second
  copy lived in the private platform repo and had re-diverged ~200 lines within
  a day of being reconciled; two engines means the control plane can author
  policy the enforcing SDK cannot execute.

### Fixed
- `pip install parapetai-agent[adk,web]` was **unsatisfiable**: the `web` extra
  pinned `starlette>=0.38,<1.0` while `google-adk>=2.7` requires
  `starlette>=1.3.1`. A developer could have the ADK integration or
  `IdentityMiddleware`, never both. The `web` bound is widened to `<2.0` --
  `identity_middleware.py` touches only `BaseHTTPMiddleware`, `Request`,
  `Response` and `ASGIApp`, the stable core unchanged in starlette 1.x.
- The heartbeat `version` field reported the **gateway's** package version
  (`parapetai-gateway`) rather than this SDK's — a copy-paste from the
  gateway's own helper. Since that package is normally absent from an embedded
  SDK, every SDK PEP reported `0.0.0-dev`. Now reports `parapetai-agent` via
  `control_plane.sdk_version()`. The same bug had been copied into
  `governance_runtime.installed_version()`; that now delegates to the one
  implementation rather than becoming a third copy.
- Three near-identical ~90-line control-plane bootstraps (in `maf.py`,
  `adk.py`, and `Governor.from_control_plane`) collapsed into
  `control_plane.bootstrap_engine()`. Three copies meant three sets of outage
  semantics, so "the agent acts as configured" could differ by which
  integration a developer picked.
- `GovernanceDenied` had two definitions (`_exceptions.py` and
  `governance_runtime.py`). Two same-named classes look identical and fail
  every `except` that caught the other; `governance_runtime` now re-exports
  the one in `_exceptions`, which needs no framework to import.

## [0.2.0]

Framework-neutral governance and cross-framework conformance.

### Added
- `Governor` — a framework-neutral `govern()` facade (`from_policy_dir`,
  `check_input` / `authorize_tool` / `check_output`, and a `@gov.tool`
  decorator) so any Python agent framework can enforce policy without a
  dedicated adapter. Built on the same `GovernanceHook` / `PolicyEngine` core as
  the Microsoft Agent Framework adapter.
- Cross-framework conformance suite proving the block happens end-to-end in the
  real runtime of the Microsoft Agent Framework, OpenAI Agents SDK, LangGraph,
  and CrewAI (`tests/test_conformance_frameworks.py`).

### Changed
- `GovernanceDenied` now lives in `parapetai_agent._exceptions` and is exported
  from the top-level package, so it can be caught without importing any
  framework-specific module. The Microsoft Agent Framework adapter re-exports the
  same class (import site unchanged).

## [0.1.1]

Initial public release of the open-source Parapet agentic-AI SDK, extracted from
the Parapet platform into its own repository.

### Added
- In-process governance middleware for the Microsoft Agent Framework
  (`GovernedAgent`, `build_middleware`).
- Cedar policy engine with pre/tool/post stage split, default-deny, fail-closed.
- Input guardrails: PII / secrets / injection / profanity scanners.
- Output evals: groundedness (lexical default, optional HHEM backend) and an SLM
  judge with rubric scoring.
- Caller identity binding (`set_identity` / `use_identity`, `IdentityMiddleware`).
- PEP <-> control-plane HTTP client: signed bundle pull, heartbeat, key
  registration (Ed25519).
- Content-free decision export over OTLP (`configure_otel`).
