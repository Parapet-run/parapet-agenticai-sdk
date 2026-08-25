"""Provider payload parsers.

Each parser turns a provider-shaped request body into a normalised snapshot the
policy layer can reason about. The snapshot field names follow AGT's ACS
snapshot contract so policies stay portable between this gateway and an
in-process ACS SDK.

INVARIANT: a parser that does not recognise a payload returns a snapshot with
`parsed=False`. The caller MUST treat that as a reason to use the coarse
`http_request` action -- never as permission to skip evaluation. A provider
adding a field must not silently become an implicit allow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Snapshot:
    """Normalised view of one inbound request.

    identity_claims: verified identity attributes (e.g. Entra oid/tid/roles),
    distinct from the HTTP path's caller.agent_id (parapetai_agent.identity), which
    is an unverified, spoofable claim by design (ADR 0003). Empty for every
    existing HTTP-parsed request -- no parser here populates it; it exists so
    a caller with real verified identity (e.g. the MAF in-process integration,
    parapetai-agent/src/parapetai_agent/maf.py, fed by an Entra-authenticated caller)
    can pass it through to Cedar as scalar record attributes
    (context.identity_claims.oid, .tid, ...) for AST-style identity-aware
    policy, without requiring a schema change per identity provider -- it's a
    flat dict[str, str], not a fixed set of named fields.
    """

    provider: str
    endpoint: str
    model: str | None = None
    parsed: bool = False
    messages_preview: str = ""  # first N chars, for injection-style rules
    declared_tools: list[str] = field(default_factory=list)
    tool_name: str | None = None  # set only for genuine tool invocations
    tool_args: dict[str, Any] = field(default_factory=dict)
    stream: bool = False
    raw_size: int = 0
    # Post-call only (see parapetai_agent.policy.hooks / parapetai-agent's maf.py):
    # first N chars of a model's response text / a tool's stringified
    # result, for a stage="post" Cedar decision to reason about. Never
    # populated by an HTTP-parsed request -- there is no response parser,
    # only in-process callers building a Snapshot from an already-returned
    # ChatResponse/tool result populate these. Same truncation convention
    # as messages_preview (first N chars, not the full content) and same
    # invariant-10 handling: a caller's audit function must strip these
    # from what gets logged, exactly as it already does for
    # messages_preview.
    response_preview: str = ""
    tool_result_preview: str = ""
    identity_claims: dict[str, str] = field(default_factory=dict)
    # Separate from identity_claims (a flat dict of scalar attributes) because
    # a role claim is a SET, not a scalar -- Cedar has real Set<String>
    # support with .contains()/.containsAny(), and _scalarise() in
    # policy/engine.py only preserves list-ness for TOP-LEVEL context fields
    # (a list nested inside identity_claims would get JSON-stringified
    # instead, losing .contains() support). Populated from an OIDC/JWT
    # `roles` claim (e.g. Entra ID app roles) by whoever authenticated the
    # caller -- this dataclass does not validate a token itself.
    identity_roles: list[str] = field(default_factory=list)

    def to_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "parsed": self.parsed,
            "stream": self.stream,
            "raw_size": self.raw_size,
        }
        if self.model:
            ctx["model"] = self.model
        if self.declared_tools:
            ctx["declared_tools"] = self.declared_tools
        if self.messages_preview:
            ctx["messages_preview"] = self.messages_preview
        if self.tool_name:
            ctx["tool_name"] = self.tool_name
            ctx["tool_args"] = self.tool_args
        if self.response_preview:
            ctx["response_preview"] = self.response_preview
        if self.tool_result_preview:
            ctx["tool_result_preview"] = self.tool_result_preview
        if self.identity_claims:
            ctx["identity_claims"] = self.identity_claims
        # NOT `if self.identity_roles:` -- a real, previously-shipped bug:
        # a genuinely authenticated caller with zero roles (identity_roles
        # == [], a normal and expected state, not a missing-identity one)
        # would have this key silently OMITTED (empty list is falsy),
        # making Cedar's `context has identity_roles` check indistinguishable
        # from "no identity asserted at all" -- which lets a role-gated
        # `forbid` (policies/30-identity.cedar) never activate and silently
        # ALLOWS a role-less user through, the opposite of what a role gate
        # should do. Include identity_roles (even empty) whenever ANY
        # identity was asserted (identity_claims present) -- verified by a
        # live concurrent-task test that caught exactly this.
        if self.identity_claims or self.identity_roles:
            ctx["identity_roles"] = self.identity_roles
        return ctx

    @property
    def action(self) -> str:
        if self.tool_name:
            return "tool_call"
        if not self.parsed:
            return "http_request"  # coarse fallback, never an allow shortcut
        return "model_call"


class Parser(Protocol):
    provider: str

    def matches(self, path: str) -> bool: ...
    def parse(self, path: str, body: Any) -> Snapshot: ...


PREVIEW_CHARS = 2000


def _preview(parts: list[str]) -> str:
    return " ".join(parts)[:PREVIEW_CHARS]


class OpenAIParser:
    """OpenAI Chat Completions and Responses. Also covers every
    OpenAI-compatible provider and LiteLLM in openai/ mode."""

    provider = "openai"

    def matches(self, path: str) -> bool:
        return path.startswith(
            ("/v1/chat/completions", "/v1/responses", "/v1/completions", "/v1/embeddings")
        )

    def parse(self, path: str, body: Any) -> Snapshot:
        snap = Snapshot(provider=self.provider, endpoint=path)
        if not isinstance(body, dict):
            return snap

        snap.model = body.get("model")
        snap.stream = bool(body.get("stream"))

        texts: list[str] = []
        for message in body.get("messages") or []:
            content = (message or {}).get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
        # Responses API uses `input` rather than `messages`.
        if isinstance(body.get("input"), str):
            texts.append(body["input"])
        snap.messages_preview = _preview(texts)

        for tool in body.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = (tool.get("function") or {}).get("name") or tool.get("name")
            if name:
                snap.declared_tools.append(str(name))

        snap.parsed = True
        return snap


class AnthropicParser:
    """Anthropic Messages API."""

    provider = "anthropic"

    def matches(self, path: str) -> bool:
        return path.startswith("/v1/messages")

    def parse(self, path: str, body: Any) -> Snapshot:
        snap = Snapshot(provider=self.provider, endpoint=path)
        if not isinstance(body, dict):
            return snap

        snap.model = body.get("model")
        snap.stream = bool(body.get("stream"))

        texts: list[str] = []
        if isinstance(body.get("system"), str):
            texts.append(body["system"])
        for message in body.get("messages") or []:
            content = (message or {}).get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        snap.messages_preview = _preview(texts)

        snap.declared_tools = [
            str(t["name"]) for t in body.get("tools") or [] if isinstance(t, dict) and t.get("name")
        ]
        snap.parsed = True
        return snap


class GeminiParser:
    """Google Gemini / Vertex generateContent.

    Vertex paths embed project and location:
      /v1/projects/{p}/locations/{l}/publishers/google/models/{m}:generateContent
    """

    provider = "gemini"

    def matches(self, path: str) -> bool:
        return ":generateContent" in path or ":streamGenerateContent" in path

    def parse(self, path: str, body: Any) -> Snapshot:
        snap = Snapshot(provider=self.provider, endpoint=path)
        snap.stream = ":streamGenerateContent" in path
        if "/models/" in path:
            snap.model = path.split("/models/")[-1].split(":")[0]

        if not isinstance(body, dict):
            return snap

        texts: list[str] = []
        system = body.get("systemInstruction") or {}
        for part in (system.get("parts") or []) if isinstance(system, dict) else []:
            if isinstance(part, dict) and "text" in part:
                texts.append(str(part["text"]))
        for item in body.get("contents") or []:
            for part in (item or {}).get("parts") or []:
                if isinstance(part, dict) and "text" in part:
                    texts.append(str(part["text"]))
        snap.messages_preview = _preview(texts)

        for tool in body.get("tools") or []:
            for decl in (tool or {}).get("functionDeclarations") or []:
                if isinstance(decl, dict) and decl.get("name"):
                    snap.declared_tools.append(str(decl["name"]))

        snap.parsed = True
        return snap


class MCPParser:
    """MCP over streamable HTTP (JSON-RPC 2.0).

    This is the only parser that yields a genuine *tool invocation*. LLM API
    requests carry tool declarations, not calls -- the model has not chosen
    anything yet. Enforcement on tool arguments happens here.
    """

    provider = "mcp"

    def matches(self, path: str) -> bool:
        return path.startswith("/mcp")

    def parse(self, path: str, body: Any) -> Snapshot:
        snap = Snapshot(provider=self.provider, endpoint=path)
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return snap

        method = body.get("method")
        snap.parsed = True
        if method != "tools/call":
            snap.messages_preview = str(method or "")[:200]
            return snap

        params = body.get("params") or {}
        snap.tool_name = str(params.get("name", ""))
        args = params.get("arguments")
        snap.tool_args = args if isinstance(args, dict) else {"_raw": str(args)[:4096]}
        return snap


PARSERS: tuple[Parser, ...] = (
    OpenAIParser(),
    AnthropicParser(),
    GeminiParser(),
    MCPParser(),
)


def parse_request(path: str, body: Any) -> Snapshot:
    for parser in PARSERS:
        if parser.matches(path):
            try:
                return parser.parse(path, body)
            except Exception:
                # A parser bug must not fail open. Return an unparsed snapshot
                # so the caller falls back to the coarse action.
                log.exception("parser_error", provider=parser.provider, path=path)
                return Snapshot(provider=parser.provider, endpoint=path)
    log.warning("no_parser_matched", path=path)
    return Snapshot(provider="unknown", endpoint=path)
