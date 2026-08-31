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
| `PARAPETAI_MCP_CONFIG_DIR` | `~/.parapet` | Directory where `credentials.json` (the CLI token, one entry per control-plane URL) is stored after `parapet_login`. |

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
