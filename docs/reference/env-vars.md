# Environment variables

Every environment variable read anywhere in this repo, organized by which
component reads it. None of these are required for the base
`Governor.from_policy_dir()` / local-mode path — the SDK runs fully local
with zero environment configuration. They start mattering once you opt
into a control plane, OTel export, or a specific judge/groundedness
backend.

## In-process SDK (`parapetai-agent`)

| Variable | Default | Controls |
|---|---|---|
| `PARAPETAI_CONTROL_PLANE_URL` | none | Control-plane URL for `build_middleware()`/`build_plugin()`/`Governor.from_control_plane()` when not passed as an argument. Required (paired with the secret below) to enable control-plane mode; omit both to stay fully local. |
| `PARAPETAI_AGENT_SECRET` | none | Bearer secret for control-plane auth. Required alongside the URL above — `Governor.from_control_plane()` raises `RuntimeError` immediately if either is missing and neither was passed as an argument. |
| `PARAPETAI_AGENT_ID` | `ANONYMOUS` (`build_middleware`/`build_plugin`) or `"agent"` (`Governor`) | Identifies this agent to the control plane / in the policy engine's principal. |
| `PARAPETAI_OTLP_ENDPOINT` | falls back to the control-plane URL | OTLP export endpoint override, when it differs from the control plane itself. |
| `PARAPETAI_OTEL_LOG_CONTENT` | `"false"` | Opt-in gate for whether OTel spans carry full prompt/response/tool-arg text. The decision audit record itself is **always** content-free regardless of this flag — see [Observability](../OBSERVABILITY.md). |
| `PARAPETAI_PEP_ID` | `f"pep-{hostname}-{pid}"` | Stable identity of this PEP process on the control plane's fleet dashboard. |
| `PARAPETAI_PEP_KEY_PATH` | `~/.parapetai/pep_ed25519.key` | Path to the persisted Ed25519 PEP identity private key, generated on first use. Only touched once a control plane is configured. |
| `PARAPETAI_OBSERVATION_CAPTURE` | `"true"` | Opt-out for [automatic vendor/resource/permission detection](vendor-scope-permission.md) — auto-enabled by `build_middleware()`/`build_plugin()`/`Governor.from_control_plane()` whenever a control plane is configured. Set `false` to disable both corroboration's instrumentation and observation tagging entirely; an explicit `observation_capture=` argument always wins over this. Same variable name and default the gateway also reads. |
| `PARAPETAI_MODEL_PRICING` | none | JSON object overriding/extending the built-in `$/1M token` price table used for [cumulative cost tracking](cost-tracking.md) (e.g. `{"my-custom-model": {"input": 1.0, "output": 3.0}}`). Malformed JSON is ignored wholesale — falls back to defaults rather than half-applying. Same variable name and shape as the control plane's own retrospective cost-panel rollup, so one override covers both. |
| `PARAPET_HHEM_MODEL` | `"vectara/hallucination_evaluation_model"` | HuggingFace model id for the in-process HHEM hallucination-evaluation predictor. See [Groundedness (HHEM)](../GROUNDEDNESS_HHEM.md). |
| `PARAPET_HHEM_URL` | none | If set, call a remote HHEM eval service instead of loading the model in-process. |
| `PARAPET_SLM_JUDGE_MODEL` | none | Dedicated SLM-judge model name — takes priority over reusing the agent's own model. |
| `PARAPET_SLM_JUDGE_URL` | none | Dedicated SLM-judge model base URL. |
| `PARAPET_SLM_JUDGE_KEY` | none | Dedicated SLM-judge model API key. |
| `AZURE_OPENAI_ENDPOINT` | none | Lets the response judge reuse the agent's own Azure OpenAI config instead of a dedicated judge endpoint. |
| `AZURE_OPENAI_API_VERSION` | `"2024-10-21"` | Azure OpenAI API version for the judge's Azure-flavor client. |
| `AZURE_OPENAI_API_KEY` | none | Azure key for the judge's Azure-flavor client. |
| `AZURE_OPENAI_CHAT_COMPLETION_MODEL` | none | Judge's Azure model when reusing the agent's own Azure config. |
| `OPENAI_API_KEY` | `"local"` | Fallback API key for the judge's OpenAI-flavor client. |
| `OPENAI_BASE_URL` | none | Judge's OpenAI base URL. |
| `OPENAI_CHAT_COMPLETION_MODEL` | none | Judge falls back to this if the agent has no dedicated judge model configured. |

`judge`-related vars above only apply when the `judge` extra's
`litellm`-backed judge is in use — the default `slm` backend needs none of
them.

## MCP server (`parapetai-mcp`)

| Variable | Default | Controls |
|---|---|---|
| `PARAPETAI_CONTROL_PLANE_URL` | `https://app.parapet.run` | Default control-plane URL every `parapet_*` tool uses unless a per-call argument overrides it. |
| `PARAPETAI_MCP_CONFIG_DIR` | `~/.parapet` | Directory where `credentials.json` (the CLI token, one entry per control-plane URL) is stored after `parapet_login_start`/`parapet_login_wait`. |

## Gateway (`parapetai-gateway`)

The gateway is entirely environment-driven — no config file. Full detail
in [`gateway/README.md`](https://github.com/Parapet-run/parapet-agenticai-sdk/tree/main/gateway).

| Variable | Default | Controls |
|---|---|---|
| `PARAPETAI_MODE` | `"enforce"` | Gateway enforcement mode. |
| `PARAPETAI_HOST` | `"0.0.0.0"` | Bind host. |
| `PARAPETAI_PORT` | `"8080"` | Bind port. |
| `PARAPETAI_POLICY_DIR` | `/etc/parapetai/policies` | Local Cedar policy directory — **the gateway's own local-mode directory, distinct from the in-process SDK's `policy_dir=` constructor argument.** |
| `PARAPETAI_ENTITIES_PATH` | none | Entities file path. |
| `PARAPETAI_CONTROL_PLANE_URL` | none | Control-plane URL for the gateway's PEP. Optional — the gateway runs local-only without it. |
| `PARAPETAI_AGENT_ID` | none | Which control-plane-provisioned agent this PEP is. Pairs with the secret below. |
| `PARAPETAI_AGENT_SECRET` | none | Agent secret for control-plane auth. |
| `PARAPETAI_BUNDLE_POLL_INTERVAL_S` | `"30"` | Policy bundle poll interval, seconds. |
| `PARAPETAI_OTLP_ENDPOINT` | none | OTLP export endpoint. |
| `PARAPETAI_UPSTREAM_TIMEOUT` | `"600"` | Upstream HTTP request timeout, seconds. |
| `PARAPETAI_DECISION_BUDGET_MS` | `"50"` | Policy decision time budget, milliseconds. |
| `PARAPETAI_MAX_BODY_BYTES` | `8388608` (8 MiB) | Max request body size. |
| `PARAPETAI_LOG_LEVEL` | `"info"` | Log level. |
| `PARAPETAI_CREDENTIAL_MODE` | `"passthrough"` | `passthrough` (forward the caller's own auth header) vs. `broker` (inject a gateway-held provider key). |
| `PARAPETAI_LOG_PROMPTS` | `"false"` | Whether to log prompt content as a separate, explicit `prompt_content` audit event. Opt-in only. |
| `PARAPETAI_OBSERVATION_CAPTURE` | `"true"` | Opt-out for automatically observing every proxied `tools/call` for [vendor/resource/permission detection](vendor-scope-permission.md) — on by default whenever `PARAPETAI_AGENT_ID` is set. Same variable name the in-process SDK also reads. |
| `PARAPETAI_VENDOR_SCOPED_RESOURCES` | `"false"` | Opt-in [vendor-scoped Cedar resources](vendor-calls.md), the gateway's counterpart of the `vendor_scoped_resources` flag on every in-process surface. When `true`, a `tools/call` evaluates against `Resource::"<vendor_system>/<vendor_operation>"`, or the fail-closed `Resource::"undeclared"` when no vendor is declared (so every tool call lands there unless `PARAPETAI_MCP_TOOL_MAP` classifies it). Leave `false` unless your policies already target that resource shape: existing `resource == Resource::"mcp"` rules stop matching once it is on. |
| `PARAPETAI_MCP_TOOL_MAP` | none | Inline JSON, or a path to a JSON file, declaring what each MCP tool does against which vendor: `target → tool → {vendor_system, resource_type, crud_action | crud_action_from}`. Fills `context.vendor_system` / `vendor_operation` / `crud_action` so Cedar can gate by what a tool *does*. Malformed raises at startup. See [gateway vendor mapping](vendor-calls.md#gateway-mcp-tool-mapping). |
| `PARAPETAI_REQUIRE_VERIFIED_IDENTITY` | `"false"` | When `true`, a request with no verified identity is refused (401) instead of falling back to the `/a/{agent_id}` path claim. Startup fails if no identity method is configured. See [verified identity](gateway-identity.md). |
| `PARAPETAI_IDENTITY_HEADER` | `"x-parapetai-identity"` | Header carrying the caller's IdP-issued JWT, or (if `PARAPETAI_ALLOW_SHARED_SECRET=true`) a shared secret. Not `Authorization` by default (that carries the caller's upstream credential under passthrough). Always stripped before forwarding. `authorization` conflicts with `PARAPETAI_MCP_AUTH_MODE=oauth2`. |
| `PARAPETAI_IDP_ISSUER` | none | Expected `iss` of an accepted JWT, e.g. `https://login.microsoftonline.com/<tenant>/v2.0`. Setting any `PARAPETAI_IDP_*` requires issuer, JWKS URL and audience. |
| `PARAPETAI_IDP_JWKS_URL` | none | The IdP's key-set URL. Must be `https://`. |
| `PARAPETAI_IDP_AUDIENCE` | none | Comma-separated accepted `aud` values. |
| `PARAPETAI_IDP_ALGORITHMS` | `"RS256"` | Accepted signing algorithms. `none` and `HS*` are refused at startup. |
| `PARAPETAI_IDP_AGENT_CLAIMS` | `"azp,appid,client_id"` | Ordered claims; the first present one is the caller's agent identity. |
| `PARAPETAI_IDENTITY_BINDINGS` | none | JSON file mapping a verified identity (JWT issuer+subject, mTLS CN, or a shared secret's hash) to an `agent_id`. Required once any identity method is configured; a verified identity with no binding is refused. |
| `PARAPETAI_ALLOW_SHARED_SECRET` | `"false"` | Additive to mTLS, never a replacement (mTLS stays required by default). When `true`, a caller with no client certificate and no IdP token can authenticate with a per-agent bearer secret in `PARAPETAI_IDENTITY_HEADER`. Has no effect while `PARAPETAI_TLS_CLIENT_AUTH=required` (logged as `shared_secret_unreachable`), since that refuses a non-mTLS caller's handshake before this is ever checked. See [shared secret](gateway-identity.md#shared-secret-additive-off-by-default). |
| `PARAPETAI_TLS_CERT` / `PARAPETAI_TLS_KEY` | none | The gateway's own server certificate and key. `PARAPETAI_TLS_CERT` is required when `PARAPETAI_TLS_CLIENT_CA` is set; `PARAPETAI_TLS_KEY` may be omitted only when the certificate file also contains the private key (some secrets managers export it that way). |
| `PARAPETAI_TLS_CLIENT_CA` | none | CA that signs client certificates. Setting it turns on mTLS, terminated in the gateway. |
| `PARAPETAI_TLS_CLIENT_AUTH` | `"required"` | `required` refuses a handshake with no client certificate; `optional` lets JWT-only callers share the port. |
| `PARAPETAI_HEALTH_PORT` | none | Starts a **second, plain-HTTP** listener on this port serving only `/__parapetai/health` and `/ready` (up/down, no policy details) for a kubelet or Docker probe, which cannot present a client certificate. For probes only: never map it to a Service, load balancer or ingress. |
| `PARAPETAI_TLS_RELOAD_INTERVAL_S` | `"30"` | How often (seconds) to check the mTLS files for rotation and hot-swap them in without a restart. `0` disables reload. A bad rotation is logged and ignored; the previous material keeps serving. See [rotation](gateway-identity.md#rotation-and-reload). |
| `PARAPETAI_ADMIN_ROUTES` | `"true"` | `false` removes `/__parapetai/policies`, `/policies/reload` and `/observations` from the agent-facing listener and trims `/ready` to a bare status. Set it on any listener reachable by more than the pod itself. |
| `PARAPETAI_GATEWAY_SITE` | none | An opaque label for where this gateway runs (`site-a`, `dc1`, …), supplied by the deployment and never interpreted by the gateway. Reported in the heartbeat so a control plane fronting several gateways can tell them apart. |
| `PARAPETAI_GATEWAY_CONNECTED_WINDOW_S` | `"900"` | How long (seconds) an agent identity counts as "connected" in the status report after its last request. |
| `PARAPETAI_MCP_AUTH_MODE` | `"none"` | `none` vs. `oauth2` for the `/mcp` path. |
| `PARAPETAI_MCP_OAUTH_SHARED_SECRET` | none | OAuth2 shared secret, gates `/authorize`. **Required** when `PARAPETAI_MCP_AUTH_MODE=oauth2` — the gateway fails closed at startup if it's missing. |
| `PARAPETAI_MCP_OAUTH_CODE_TTL_S` | `"300"` | OAuth2 authorization code TTL, seconds. |
| `PARAPETAI_MCP_OAUTH_TOKEN_TTL_S` | `"3600"` | OAuth2 access token TTL, seconds. |
| `PARAPETAI_MCP_UPSTREAMS` | `""` | JSON object mapping MCP target name → destination URL. Malformed JSON raises at startup — fail closed, not a silent skip. |
| `PARAPETAI_MCP_BASE_URL` | none | Single-target MCP upstream URL, used when no per-target `PARAPETAI_MCP_UPSTREAMS` entry matches. |
| `PARAPETAI_{PROVIDER}_BASE_URL` (e.g. `PARAPETAI_OPENAI_BASE_URL`, `PARAPETAI_ANTHROPIC_BASE_URL`, `PARAPETAI_GEMINI_BASE_URL`) | each provider's real API base URL | Per-provider upstream override. |
| `PARAPETAI_OPENAI_KEY` / `PARAPETAI_ANTHROPIC_KEY` / `PARAPETAI_GEMINI_KEY` / `PARAPETAI_MCP_KEY` | none | Provider credential injected in `broker` credential mode only. |
| `PARAPETAI_PEP_ID` | generated | Stable fleet-dashboard identity for this PEP process — same mechanism as the SDK's own `PARAPETAI_PEP_ID`. |

!!! warning "`PARAPETAI_POLICY_DIR` is a gateway-only variable"
    It's easy to conflate with the in-process SDK's `policy_dir=`
    constructor argument (`Governor.from_policy_dir()`,
    `GovernedAgent(policy_dir=...)`, `GovernedRunner(policy_dir=...)`) —
    they are unrelated. The SDK never reads a `policy_dir` environment
    variable; it's always an explicit argument. Setting
    `PARAPETAI_POLICY_DIR` has no effect on `Governor`/`GovernedAgent`/
    `GovernedRunner` at all.
