# Adding governance to a Microsoft Agent Framework agent

Derived by porting seven of [Microsoft Agent Framework's own `python/samples`](https://github.com/microsoft/agent-framework/tree/main/python/samples)
into `examples/maf_sample_01/` through `examples/maf_sample_07/` --
across a hello-world agent, function tools, multi-turn conversation,
memory/context providers, tool approval, framework-native middleware, and
structured output, the same wiring appeared every time. That repetition
IS the integration pattern; this doc names it once instead of
re-deriving it from each example's README.

Every `maf_sample_0N/` (`01` through `07`) now uses the same minimal
wiring described below -- `examples/maf_cli/` too, with one deliberate
exception (it keeps `policy_dir=` explicit; see its own README's "The
wiring is the same minimal shape as every other example, with one
exception"). Follow `maf_sample_01/`'s shape for new work.

**The chat client is never part of the pattern.** Every `maf_sample_0N/`
keeps the SAME client upstream used at that path -- `FoundryChatClient` +
`AzureCliCredential` for `01`/`02`/`03`/`04`/`06`, plain
`OpenAIChatClient`-family clients for `05`/`07` -- because `GovernedAgent`
is client-agnostic by construction: `ParapetChatMiddleware`/
`ParapetFunctionMiddleware` (`src/parapetai_agent/maf.py`) subclass
`agent_framework`'s own generic `ChatMiddleware`/`FunctionMiddleware`,
which operate on `ChatContext`/message data, never on the client class.
Confirmed by reading that source, not assumed, and backed by a real test
(`parapetai-agent/tests/test_maf.py::TestProviderIdentification::test_foundry`)
plus a tracked entry in `parapetai-support.yaml`. `agent-framework-foundry`
itself is confirmed to be a thin wrapper, not a separate implementation --
`FoundryChatClient`'s own MRO is `FoundryChatClient -> RawFoundryChatClient
-> RawOpenAIChatClient -> BaseChatClient`, i.e. it literally subclasses
`agent-framework-openai`'s client (Foundry's wire protocol is
OpenAI-compatible) on top of the same `agent-framework-core` foundation
every other client sits on. Swapping YOUR agent's client for a "simpler"
one to make wiring easier is never necessary and was a real mistake made
once while building this directory -- don't repeat it.

If you're integrating your own agent, the fastest path is: read this
page, then open `examples/maf_sample_01/run_example.py` and copy its
wiring, keeping YOUR client unchanged.

## The wiring

```python
from parapetai_agent import GovernedAgent  # was: from agent_framework import Agent

agent = GovernedAgent(
    client=...,           # UNCHANGED -- whatever client you already use
    name="...",            # unchanged
    instructions="...",    # unchanged
)
```

That's the entire required integration. `GovernedAgent` is a drop-in
subclass of `agent_framework.Agent` -- every other kwarg you already pass
to `Agent(...)` (`tools`, `context_providers`, your own `middleware=[...]`,
...) still works unchanged. This is deliberately a per-call-site import
swap, not a process-wide monkeypatch of `Agent.__init__` -- see
`GovernedAgent`'s own docstring for why: an agent that's silently
governed with no visible change at the construction site is a strictly
worse failure mode than one that requires touching the import.

With zero further configuration, this already enforces real Cedar policy
(base permits, `permit(principal, action == Action::"model_call",
resource)` and similar) -- the policy set bundled in `parapetai-agent`
(`src/parapetai_agent/policy/default_policies/`, a real,
read-only file co-located with `PolicyEngine`, the class that loads it --
NOT `parapetai-agent`, despite `GovernedAgent`/`build_middleware()` being
the only current consumer: this is generic "safe Cedar starting point"
content, not something specific to being embedded in an agent framework,
so it lives next to the engine, available to any future consumer of
`parapetai-agent`'s policy engine, gateway's own standalone PEP included), not
an empty/no-op engine and not a required setup step. Confirmed live:
this needs no writable filesystem at all, so it works unmodified in a
read-only container (k8s, serverless) with no mounted volume.

## The optional kwargs, and what each defaults to

```python
agent = GovernedAgent(
    client=..., name="...", instructions="...",
    policy_dir=None,             # -> the bundled default above
    entities_path=None,          # -> bundled entities.json (or none, if you gave your own policy_dir with no entities.json alongside it)
    agent_id=None,                # -> PARAPETAI_AGENT_ID env var, else Agent::"anonymous" (still default-deny, never a bypass)
    control_plane_url=None,       # -> PARAPETAI_CONTROL_PLANE_URL env var
    agent_secret=None,            # -> PARAPETAI_AGENT_SECRET env var
    persist_policy_dir=None,      # -> a control-plane-pulled bundle stays in memory only
    local_log_dir=None,           # -> no local rotating audit-log file
    persist_pep_key=True,         # -> this PEP's Ed25519 identity IS persisted to disk (see below)
)
```

- **`policy_dir`/`entities_path`**: pass your own to override the
  bundled default with local Cedar policies (no control plane).
- **`agent_id`/`control_plane_url`/`agent_secret`**: set the latter two
  (both together) to pull a REAL control-plane-provisioned agent's
  bundle. `GovernedAgent.__init__` then calls `build_middleware()`,
  which registers this process's Ed25519 identity, pulls the bundle, and
  spawns a background thread that keeps polling and heartbeating.
  Partially set (e.g. a URL with no secret) is treated as "not using the
  control plane," never attempted-and-half-failed.
- **`persist_policy_dir`**: the pulled bundle is applied to the running
  engine's memory either way -- this only controls whether it's ALSO
  written to disk. Omitted (the default): memory only, nothing written
  anywhere, the right choice for a process with no writable/persistent
  volume; if the very first pull fails (control plane unreachable on a
  cold start), the engine keeps enforcing `policy_dir` (the bundled
  default, or your own override) instead of crashing, picked up by the
  background poller's next successful cycle. Given a real directory: the
  original behavior, a synchronous fetch-and-write there before
  constructing the engine, failing closed (raising) if that first fetch
  fails with nothing on disk yet -- for a process that DOES have
  somewhere durable to write and wants a real on-restart cache.
- **`local_log_dir`**: opt-in, idempotent-per-directory rotating
  JSON-lines decision audit log (in addition to the structlog console
  output that happens regardless). One kwarg instead of a separate
  `configure_rotating_audit_log()` call. Local file writes are always
  immediate (Python's own `logging.Handler.emit()` flushes per record) --
  nothing here to lose on a process exit.
- **`otel_log_mode`**: OpenTelemetry is wired up FOR you, automatically,
  on the same condition as the control-plane bundle pull above -- once
  `control_plane_url`/`agent_secret` both resolve, `GovernedAgent`/
  `build_middleware()` calls `configure_otel(...)` internally, so real
  spans/LogRecords (`parapetai.model_call`/`parapetai.tool_call`) ship to that
  control plane's OTLP receiver with zero extra code. `otel_log_mode` is
  the ONE knob this exposes: `"buffered"` (the default) batches
  spans/LogRecords up to `batch_max_size` (512) records OR
  `batch_schedule_delay_s` (120s -- two minutes) seconds, whichever comes
  first, before sending; `"streaming"` sends each one immediately
  instead -- lower control-plane request volume vs. lower latency. Real
  bug, found live building `examples/maf_webapp/`: Cedar enforcement
  worked fine with a control plane configured but no `configure_otel()`
  call, and every decision produced ZERO spans/logs anywhere outside the
  local structlog console/`local_log_dir` -- this auto-wiring closes
  that gap by construction, the same reasoning `GovernedAgent` itself
  exists for (forgetting a required call silently drops enforcement/
  observability). No control plane configured: nothing is auto-wired
  (nowhere to ship to; `local_log_dir`/console output are unaffected
  either way).
  Need more than that -- a custom `service_name`, Azure Monitor export,
  disabled console output, custom batch tuning -- call `configure_otel(...)`
  explicitly yourself, BEFORE constructing `GovernedAgent`/calling
  `build_middleware()`; whichever `configure_otel()` call happens FIRST
  wins (OTel's own `TracerProvider`/`LoggerProvider` registration is
  process-wide and set-once), so your explicit call is never silently
  overridden by the auto-wiring -- see `examples/maf_webapp/web_app.py`'s
  `lifespan()` for exactly that (its own `configure_otel()` call, for
  Azure Monitor export and a custom `service_name`, runs before
  `build_middleware()`'s priming call). Either way, `configure_otel()`
  registers an `atexit` hook that flushes any buffered-but-not-yet-sent
  telemetry on normal process exit; a long-running server should also
  call the exported `flush_otel()` explicitly from its OWN shutdown
  sequence (`atexit` doesn't fire on SIGTERM) -- see
  `examples/maf_webapp/web_app.py`'s `lifespan()` for exactly that.
- **`persist_pep_key`**: `False` (default `True`) skips disk for this
  PEP's Ed25519 identity too -- a fresh, never-written keypair every
  construction (`parapetai_agent.pep_identity.generate_ephemeral_keypair()`),
  for a process with no writable filesystem at all. `pep_key_path` is
  ignored in that case. Trades identity STABILITY across restarts (a
  control-plane-initiated "trigger rotation" becomes a no-op -- there's
  nothing durable to rotate) for that, not correctness: signatures still
  verify, requests still get signed, this PEP just re-registers a new
  key every cold start instead of reusing one. Prefer leaving this
  `True` whenever ANY writable path is available, even an ephemeral one
  that resets between cold starts (e.g. AWS Lambda's `/tmp`) -- that
  still gets a stable identity for the lifetime of one warm container.
  shape, including Azure Monitor export.

## Identity -- one call, regardless of what shape it's in

**Setting identity is fully decoupled from calling `agent.run()`.**
Construction (`GovernedAgent(...)`) picks the policy; identity is set
separately, around the call, and `agent.run()` itself never changes --
same method, same signature, whether or not anything set an identity.
Skip identity entirely and Cedar evaluates against EMPTY
`identity_claims`/`identity_roles` -- for a policy that checks identity
(a role gate), that's a **deny**, Cedar's own default-deny working
exactly as designed, not a bypass or a skipped check.

`governed_identity()` is the one call for setting it, regardless of what
form your identity data is in -- it dispatches internally to
`current_identity()`/`identity_from_bearer_token()`/
`identity_from_azure_credential()` based on which single kwarg you pass,
so you never have to pick the matching function by hand:

```python
# claims/roles already parsed
with governed_identity(claims={"oid": "..."}, roles=["OrderViewer"]):
    result = await agent.run(...)

# a raw bearer token
with governed_identity(token=jwt):
    result = await agent.run(...)

# an azure-identity credential -- e.g. the SAME one already passed to
# FoundryChatClient (maf_sample_01/02/03/04/06) -- no second login, no
# separate token-fetch logic
credential = AzureCliCredential()
async with GovernedAgent(client=FoundryChatClient(credential=credential), ...) as agent:
    with governed_identity(credential=credential):
        result = await agent.run(...)
```

Fails loud on ambiguity, not silent: zero sources given, or more than
one, both raise `ValueError` immediately -- if there's genuinely no
identity to assert, call `agent.run()` directly, unwrapped, rather than
this function with nothing (or contradictory things) in it.

Confirmed live: an `az login`-issued Azure AD access token is a real
JWT, and its standard claims (`oid`, `upn`/`preferred_username`, `tid`,
...) are the same shape `identity_from_bearer_token()` already decodes --
the `credential=` path is a thin wrapper reusing that existing, tested
path, not new parsing logic. Identity is only relevant once a real Cedar
policy needs to know WHO is signed in (e.g. a role gate), not just WHICH
agent is calling -- every example works without it.

## Session/identity store persistence

`parapetai_agent.identity_store` (`set_identity()`/`use_identity()`/
`configure_identity_store()`) already has a pluggable seam for this --
the default (`InMemoryIdentityStore`) is fine for a single-process
script or a sticky-routed single replica; swap in a real shared backend
(Redis, a database) via `configure_identity_store()` once this agent
runs as more than one replica, since an in-memory store is invisible
across replicas (silently reverts to anonymous, not an error -- Cedar's
default-deny means that fails closed, but it's still a correctness bug
worth avoiding on purpose). `maf_sample_01/` shows the seam as a
commented-out example, since a single hello-world script has no actual
multi-replica need for one; `examples/maf_cli/run_example.py` is the
real, runnable multi-invoker version.

## What does NOT need to change

Everything else -- the chat client itself (any `agent_framework`-compatible
client works unmodified, including ones needing a real Azure identity),
tools, context providers, sessions, your own framework-native middleware
(which runs *after* Cedar's governance middleware, not instead of it --
`GovernedAgent` prepends its own `[chat_mw, func_mw]` ahead of whatever
you pass via `middleware=`), streaming vs. non-streaming `agent.run()`,
structured output via `options={"response_format": ...}` -- none of it
is aware that Cedar is in the loop. Governance is a wrapper around the
model/tool call boundary, not a rewrite of how you use the framework.

## Provisioning a real agent identity

`agent_id`/`control_plane_url`/`agent_secret` come from a real,
control-plane-provisioned agent, not values you invent. Provision one
from the control plane's dashboard (`/a/{account_id}/agents/new`); its
agent detail page prints the exact block to paste into `.env`, under
"Integrating this agent" -- the same instructions this doc describes
generically, rendered with that specific agent's real values.

## Reference implementations

- `examples/maf_sample_01/` -- the current, minimal pattern (this doc).
- `examples/maf_sample_02/` through `examples/maf_sample_07/` -- each a
  minimal, single-concept port of one upstream MAF sample, same wiring
  as `01`.
- `examples/maf_webapp/` -- the fuller picture: a long-running web
  server, `IdentityMiddleware` wiring ambient identity around each HTTP
  request, MCP tool sources, real Entra ID login, OTel export.
- `examples/maf_cli/` -- several distinct invokers/identities in one
  process, via `parapetai_agent.identity_store`'s `set_identity()`/
  `use_identity()` instead of `IdentityMiddleware`; the one example that
  keeps `policy_dir=` explicit (this repo's own role-gate policy is the
  point of that example).
