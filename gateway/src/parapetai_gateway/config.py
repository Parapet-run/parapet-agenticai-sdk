"""Gateway configuration. Environment-driven; no config file required."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from parapetai_agent.control_plane import default_pep_id


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


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


settings = Settings()
