"""Gateway configuration. Environment-driven; no config file required."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from parapetai_agent.control_plane import default_pep_id


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_mcp_upstreams(raw: str) -> dict[str, str]:
    """PARAPETAI_MCP_UPSTREAMS is a JSON object mapping a target name (the
    path segment after /mcp/, e.g. /a/<agent>/mcp/jira -> "jira") to the
    COMPLETE destination URL for that downstream MCP server -- not a base
    URL a path gets appended to (see mcp_upstream_for()'s docstring for why:
    a Streamable HTTP MCP server has exactly one endpoint, so there is no
    sub-path left to append). Malformed JSON or a non-string value fails
    closed at startup (this is read once, at process start, via
    default_factory) rather than silently running with zero configured
    targets -- the same "misconfiguration must not look like an empty-but-
    valid config" reasoning as create_app()'s oauth2-without-secret check.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PARAPETAI_MCP_UPSTREAMS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
        raise ValueError("PARAPETAI_MCP_UPSTREAMS must be a JSON object of string -> string")
    return {str(k): v.rstrip("/") for k, v in parsed.items()}


@dataclass(frozen=True, slots=True)
class Upstream:
    name: str
    base_url: str
    auth_header: str
    credential_env: str


DEFAULT_UPSTREAMS: dict[str, Upstream] = {
    "openai": Upstream("openai", "https://api.openai.com", "Authorization", "PARAPETAI_OPENAI_KEY"),
    "anthropic": Upstream(
        "anthropic", "https://api.anthropic.com", "x-api-key", "PARAPETAI_ANTHROPIC_KEY"
    ),
    "gemini": Upstream(
        "gemini",
        "https://generativelanguage.googleapis.com",
        "x-goog-api-key",
        "PARAPETAI_GEMINI_KEY",
    ),
    # No sensible global default -- unlike the LLM providers above, "the"
    # MCP upstream is whichever downstream MCP server this gateway instance
    # fronts, which differs per deployment. base_url is a placeholder
    # (upstream_for() only reaches it if PARAPETAI_MCP_BASE_URL is unset,
    # which would otherwise 502 exactly as before this entry existed --
    # see docs/mcp-interception.md's "gap this investigation surfaced").
    # auth_header/credential_env are unused in passthrough mode (the
    # default; see PARAPETAI_CREDENTIAL_MODE) since the caller's own
    # Authorization header rides through unchanged.
    "mcp": Upstream("mcp", "", "Authorization", "PARAPETAI_MCP_KEY"),
}


@dataclass(frozen=True, slots=True)
class Settings:
    mode: str = field(default_factory=lambda: os.getenv("PARAPETAI_MODE", "enforce").lower())
    host: str = field(default_factory=lambda: os.getenv("PARAPETAI_HOST", "0.0.0.0"))  # noqa: S104
    port: int = field(default_factory=lambda: int(os.getenv("PARAPETAI_PORT", "8080")))
    policy_dir: str = field(
        default_factory=lambda: os.getenv("PARAPETAI_POLICY_DIR", "/etc/parapetai/policies")
    )
    entities_path: str | None = field(
        default_factory=lambda: os.getenv("PARAPETAI_ENTITIES_PATH") or None
    )
    control_plane_url: str | None = field(
        default_factory=lambda: os.getenv("PARAPETAI_CONTROL_PLANE_URL") or None
    )
    # Which control-plane-provisioned agent this PEP is. Optional: with no
    # agent_id/agent_secret, the gateway runs exactly as before -- local
    # policy_dir only, no control-plane polling. See parapetai_agent.control_plane.
    agent_id: str | None = field(default_factory=lambda: os.getenv("PARAPETAI_AGENT_ID") or None)
    agent_secret: str | None = field(
        default_factory=lambda: os.getenv("PARAPETAI_AGENT_SECRET") or None
    )
    bundle_poll_interval_s: float = field(
        default_factory=lambda: float(os.getenv("PARAPETAI_BUNDLE_POLL_INTERVAL_S", "30"))
    )
    # Identifies this PEP PROCESS (not the agent it enforces for) on the
    # control plane's fleet dashboard -- distinct from agent_id, which can
    # be shared across many PEP processes enforcing the same agent
    # identity. Stable for the process lifetime; see
    # parapetai_agent.control_plane.default_pep_id for the generated fallback shape.
    pep_id: str = field(default_factory=default_pep_id)
    otlp_endpoint: str | None = field(
        default_factory=lambda: os.getenv("PARAPETAI_OTLP_ENDPOINT") or None
    )
    upstream_timeout: float = field(
        default_factory=lambda: float(os.getenv("PARAPETAI_UPSTREAM_TIMEOUT", "600"))
    )
    decision_budget_ms: float = field(
        default_factory=lambda: float(os.getenv("PARAPETAI_DECISION_BUDGET_MS", "50"))
    )
    max_body_bytes: int = field(
        default_factory=lambda: int(os.getenv("PARAPETAI_MAX_BODY_BYTES", str(8 * 1024 * 1024)))
    )
    log_level: str = field(default_factory=lambda: os.getenv("PARAPETAI_LOG_LEVEL", "info"))
    # passthrough: forward the caller's own Authorization/x-api-key header to
    # upstream unchanged -- the gateway never holds a real provider credential.
    # broker (opt-in, for later): strip the caller's header and inject the
    # gateway-held PARAPETAI_<PROVIDER>_KEY instead. See docs/adr/0003.
    credential_mode: str = field(
        default_factory=lambda: os.getenv("PARAPETAI_CREDENTIAL_MODE", "passthrough").lower()
    )
    # Off by default: prompt content is sensitive (PII, secrets, proprietary
    # data). Opt-in for policy authoring/tuning, logged as a distinct
    # "prompt_content" event, separate from the "decision" audit log, which
    # never carries content regardless of this setting. See docs/adr/0005.
    log_prompts: bool = field(
        default_factory=lambda: os.getenv("PARAPETAI_LOG_PROMPTS", "false").lower() == "true"
    )
    # "none" (default): the /mcp path is reachable with no bearer credential at
    # all -- today's behaviour, unchanged. Cedar is still the real gate either
    # way (agent_id is an unverified path claim regardless of this setting,
    # see proxy()'s own comment); this only controls whether an OAuth 2.1
    # handshake is required in front of it. "oauth2": required for an MCP
    # client that enforces the MCP Authorization spec against a remote server
    # it doesn't operate itself (e.g. Atlassian Rovo's custom/external MCP
    # server requirements) -- see mcp_oauth.py.
    mcp_auth_mode: str = field(
        default_factory=lambda: os.getenv("PARAPETAI_MCP_AUTH_MODE", "none").lower()
    )
    # Required when mcp_auth_mode == "oauth2" (checked at startup, fail closed
    # rather than silently running an authorization server anyone can complete
    # DCR + /authorize against with zero credential). Gates the /authorize
    # step only -- it is a deployment-operator secret, not a per-user one;
    # there is no per-user identity system here. See mcp_oauth.py's module
    # docstring for why this is sufficient given Cedar remains the real
    # authorization decision regardless of OAuth identity.
    mcp_oauth_shared_secret: str | None = field(
        default_factory=lambda: os.getenv("PARAPETAI_MCP_OAUTH_SHARED_SECRET") or None
    )
    mcp_oauth_code_ttl_s: float = field(
        default_factory=lambda: float(os.getenv("PARAPETAI_MCP_OAUTH_CODE_TTL_S", "300"))
    )
    mcp_oauth_token_ttl_s: float = field(
        default_factory=lambda: float(os.getenv("PARAPETAI_MCP_OAUTH_TOKEN_TTL_S", "3600"))
    )
    # The gateway is a reverse proxy for an ARBITRARY set of downstream MCP
    # servers, not a fixed single one -- an agent's actual tools live behind
    # however many distinct MCP servers it uses, each possibly requiring its
    # own separate Rovo "external MCP server" registration. PARAPETAI_MCP_BASE_URL
    # (via upstream_for("mcp")) remains the single-target path: hitting
    # bare /a/{agent_id}/mcp with no further segment. Naming a target here
    # (/a/{agent_id}/mcp/{target}) routes to a DIFFERENT configured
    # downstream per target, all under one gateway deployment/policy set/
    # control-plane identity -- see mcp_upstream_for()'s own docstring for
    # exactly how a target name resolves to a destination.
    mcp_upstreams: dict[str, str] = field(
        default_factory=lambda: _parse_mcp_upstreams(os.getenv("PARAPETAI_MCP_UPSTREAMS", ""))
    )

    @property
    def oauth_enabled(self) -> bool:
        return self.mcp_auth_mode == "oauth2"

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @property
    def brokering_credentials(self) -> bool:
        return self.credential_mode == "broker"

    def upstream_for(self, provider: str) -> Upstream | None:
        override = os.getenv(f"PARAPETAI_{provider.upper()}_BASE_URL")
        base = DEFAULT_UPSTREAMS.get(provider)
        if base is None:
            return None
        if override:
            return Upstream(base.name, override.rstrip("/"), base.auth_header, base.credential_env)
        # "mcp" (and any future provider with no sensible global default)
        # ships with base_url == "" -- present in DEFAULT_UPSTREAMS so the
        # override above is actually consulted, but not usable on its own.
        # Same clean 502 as an unknown provider, not a crash from building
        # a request against a blank host.
        if not base.base_url:
            return None
        return base

    def mcp_upstream_for(self, target: str | None) -> Upstream | None:
        """Resolves the downstream MCP server for one request.

        `target` is the path segment after /mcp/ (None for a bare /mcp
        request). With a target: looked up in mcp_upstreams ONLY -- an
        unrecognised target is a clean 502 (fail closed; it must never
        silently fall back to the single-upstream default, which would
        route a caller who named "servicenow" at whatever server happens to
        be configured for bare /mcp instead).

        EITHER WAY, the resolved base_url is the COMPLETE destination --
        proxy() forwards to it with no further path appended (see its own
        comment). This is deliberately NOT upstream_for()'s append-a-path
        convention (right for openai/anthropic/gemini, whose base_url is a
        host and the caller's path selects a REST resource on it): a
        Streamable HTTP MCP server exposes exactly one endpoint, so there is
        nothing meaningful to append, and doing so anyway produced a real,
        confirmed-live bug -- PARAPETAI_MCP_BASE_URL=".../mcp" plus an
        appended "/mcp" request path silently requested ".../mcp/mcp" and
        404'd. PARAPETAI_MCP_BASE_URL must therefore be the server's whole
        URL (".../mcp", not just its host), matching PARAPETAI_MCP_UPSTREAMS'
        values and this repo's own README/.env.example, which already
        documented it that way before this method's behavior was fixed to
        match.
        """
        if target is not None:
            base_url = self.mcp_upstreams.get(target)
            if base_url is None:
                return None
            return Upstream("mcp", base_url, "Authorization", "PARAPETAI_MCP_KEY")
        override = os.getenv("PARAPETAI_MCP_BASE_URL")
        if not override:
            return None
        return Upstream("mcp", override.rstrip("/"), "Authorization", "PARAPETAI_MCP_KEY")


settings = Settings()
