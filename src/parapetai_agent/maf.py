"""In-process governance for Microsoft Agent Framework (MAF).

Part of the `parapetai-agent` package -- open source, the thing an agent
framework integrator imports directly into their own process. Unlike the
standalone gateway (base-URL interception, ADR 0002, `parapetai_gateway.server`,
still in the closed/product-side `gateway/` package), this module runs
INSIDE the agent process via MAF's own middleware hooks (ChatMiddleware,
FunctionMiddleware) -- there is no HTTP request, no path prefix, no wire
bytes. It builds the same Snapshot/Cedar-evaluation contract the HTTP
path uses (`parapetai_agent.providers.parsers.Snapshot`,
`parapetai_agent.policy.engine.PolicyEngine` -- the shared, open foundation
both this module and the standalone gateway depend on), just populated
differently. See docs/maf-in-process-integration.md for the full
investigation this is built from, and spike/maf_mcp_check/ for the
earlier, non-enforcing spike that established the interception point is
real before this module tried to enforce anything through it.

Requires the `agent_framework` package -- install via the `maf` extra
(`pip install parapetai-agent[maf]`). Never imported by `parapetai_gateway.server.app`;
opt-in, matching CLAUDE.md's "interop is optional" rule.

## What FunctionInvocationContext alone can and can't give you

Verified empirically, not assumed from docs:

  Available directly on FunctionInvocationContext:
    tool_name  <- context.function.name
    tool_args  <- context.arguments

  NOT available on FunctionInvocationContext -- these belong to ChatContext,
  which fires once per model call, not once per tool call:
    model, declared_tools, messages_preview, stream, provider (derived from
    type(ChatContext.client))

  context.metadata is NOT shared across middleware *types* -- ChatContext,
  FunctionInvocationContext, and AgentContext each get a fresh metadata dict
  per invocation (confirmed by comparing id(context.metadata) across a live
  run: three different ids for one tool-calling turn). The generic MAF docs
  phrase "for storing additional data between middleware" means between
  multiple middleware of the *same* type in a chain, not across
  ChatMiddleware/FunctionMiddleware. Correlating "which model call preceded
  this tool call" therefore needs an explicit mechanism: a
  contextvars.ContextVar here, safe under concurrent agent.run() calls
  sharing one Agent/middleware instance (each asyncio task gets its own
  isolated view; a plain instance attribute would leak across concurrent
  runs).

### _current_chat is not scoped to this method

A real, previously-shipped bug: ParapetChatMiddleware.process() used to
`.set()` the correlation and `.reset()` it in a `finally` block before
returning -- the ordinary "clean up a ContextVar after yourself" pattern.
That's wrong here specifically: FunctionMiddleware fires as a SEPARATE,
LATER call from MAF's own run loop, only after ChatMiddleware.process() has
already returned (verified: they are sequential SIBLING calls, not nested
-- see "Enforcement asymmetry" isn't the only place this matters). A
reset-in-finally therefore cleared _current_chat back to its default
(None) before ParapetFunctionMiddleware ever got a chance to read it,
silently: every tool_call decision's context showed `"provider": "unknown"`
instead of the real provider, every single time, for as long as this
module has existed, and it would have silently broken tool_call span
parenting too once that was added. _current_chat is now .set() and left
alone; it persists until the NEXT model_call naturally overwrites it,
which is what actually matches MAF's real execution order.

## Enforcement asymmetry -- verified, not assumed, and load-bearing

Raising from ChatMiddleware.process() propagates as a real Python exception
all the way to the agent.run() caller, and the underlying HTTP call to the
provider never happens (confirmed: zero requests reached a canary upstream
when denied). This is exactly as strong as the HTTP gateway's 403 -- use it
for model_call denial.

Raising from FunctionMiddleware.process() does NOT do the same thing: MAF's
own function-invocation loop catches it and converts it into a generic tool
*error result* fed back to the model ("Function 'x' raised an exception;
returning an error result to the model") -- the run does not stop, and nothing
guarantees the model faithfully reports that error rather than continuing as
if nothing happened. Verified live: a raising FunctionMiddleware still
produced "RESULT: Order 12345 has shipped." from the driving conversation.
Not calling call_next() and substituting context.result *does* reliably
prevent the underlying tool code/RPC from ever running (verified: a
module-level side-effect flag stayed False; for an MCP-sourced tool, the
server's own log showed no CallToolRequest at all) -- that's the strongest
guarantee available at this specific hook, but "the caller sees the block
happened" is weaker than at the chat layer: it's a string in the
conversation, not a raised exception. This asymmetry is real, not a
placeholder to fix later -- see docs/maf-in-process-integration.md.

## Identity

Snapshot.identity_claims (parapetai-agent/src/parapetai_agent/providers/parsers.py)
carries verified identity attributes, distinct from the HTTP path's
unverified caller.agent_id (parapetai_agent.identity, ADR 0003).
Snapshot.identity_roles is a
separate field for a role SET (Cedar Set<String>, supports .contains()) --
identity_claims is a flat dict of scalar attributes and would JSON-stringify
a nested list, losing that. Nothing here validates a token, it trusts
whatever the caller already verified (e.g. an Entra ID token's claims,
decoded by examples/maf_webapp/entra_login.py) and passes
them through as Cedar context attributes.

Two ways to hand identity to this module, checked in this order:

  1. Explicitly, per call: agent.run(function_invocation_kwargs=
     {"identity_claims": {...}, "identity_roles": [...]}). Wins if
     present.
  2. Ambiently, via current_identity(claims=..., roles=...) (a context
     manager) or set_current_identity()/reset_current_identity() (the
     token-based pair it's built on, for callers whose set and reset
     happen in different places -- e.g. a web framework's request/response
     hooks). Set once -- typically right after validating a token, at the
     top of a request handler -- and every governed decision made from
     there on picks it up automatically, without repeating
     function_invocation_kwargs on every single agent.run() call. Backed
     by a contextvars.ContextVar, the same mechanism _current_chat already
     uses, so it's correctly isolated per asyncio task under concurrent
     requests, not a process-global that would leak one user's identity
     into another's decision.

Nothing here can discover identity from thin air -- verified directly, not
assumed: agent_framework's AgentSession (the one plausible ambient source)
carries only session_id/service_session_id, nothing identity-shaped. It has
to enter from the embedding application at least once; what option 2
removes is having to re-enter it on every call.

Where these land is NOT the same attribute on both context types --
verified directly against agent_framework's ChatContext/
FunctionInvocationContext source, not assumed, after finding this the hard
way (see ParapetChatMiddleware.process()'s inline comment for the full
story): FunctionInvocationContext.kwargs receives
function_invocation_kwargs directly, but ChatContext has BOTH a .kwargs
(populated from Agent.run(client_kwargs=...), unrelated) and a SEPARATE
.function_invocation_kwargs (populated from
Agent.run(function_invocation_kwargs=...), the one that actually matters
here). Reading ChatContext.kwargs here was a real, previously-shipped bug:
identity never reached a model_call Cedar decision in any real Agent.run(),
only in a test that constructed ChatContext by hand instead of driving it
through Agent.run(). Fixed to read .function_invocation_kwargs instead.

See docs/maf-in-process-integration.md for the Entra ID token claim shape
this was checked against, and examples/maf_webapp/ for a live
Entra device-code login feeding real identity_claims/identity_roles through
this exact path.

## Two identities, not one -- which is which matters for telemetry

Every decision carries TWO distinct identities, and conflating them is a
real mistake to avoid when reading logs/traces, not a pedantic distinction:

  record["principal"] (Cedar's principal, e.g. Agent::"example-entra") is
  the AGENT's own identity -- a static string the embedding application
  chose when it called build_middleware(agent_id=...). It does not change
  per request and has nothing to do with which human, if any, is behind a
  given call.

  identity_claims/identity_roles (context.identity_claims.oid,
  context.identity_roles) is the END USER's identity -- e.g. Bob, verified
  via a real Entra sign-in in examples/maf_webapp/
  entra_login.py -- passed in per-call via
  agent.run(function_invocation_kwargs={...}) and therefore CAN change
  every request, independently of which agent_id is running.

configure_otel() (below) keeps this distinction in the telemetry it emits:
the agent identity stays in Resource-adjacent/record-level data (it
describes the PROCESS), while the end user's oid/roles are mapped
specifically onto the enduser.id/user.roles OpenTelemetry semantic
convention attributes on each LogRecord and Span -- the ones Azure
Monitor gives special column treatment to (user_AuthenticatedId), not
just another key inside a JSON blob.

## OpenTelemetry / Azure Monitor compatibility

_audit() previously only emitted a flat structlog JSON dict -- not OTel:
no TraceId/SpanId, no Resource, no distinguished Body vs Attributes, no
normalised SeverityNumber (verified directly against the OTel Log Data
Model spec and against Microsoft's own OTel-to-AppTraces field mapping
docs, not assumed). configure_otel() closes that gap: real spans
(parapetai.model_call / parapetai.tool_call, with tool_call spans explicitly
linked as children of the model_call span that triggered them -- they are
NOT naturally nested, since ChatMiddleware and FunctionMiddleware fire as
sequential sibling calls from MAF's own run loop, verified, not assumed)
and real LogRecords via the OTel Logs SDK, exportable to Azure Monitor via
azure-monitor-opentelemetry-exporter when a connection string is
supplied. The rotating-file JSON audit trail (configure_rotating_audit_log)
is unaffected -- both can run at once, independently.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog
from agent_framework import (
    Agent,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    FunctionInvocationContext,
    FunctionMiddleware,
    Message,
)
from azure.core.credentials import TokenCredential
from opentelemetry import trace
from opentelemetry.context import Context as OtelContext
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode

from parapetai_agent import pep_identity
from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.control_plane import bootstrap_engine
from parapetai_agent.governance_runtime import GovernanceDenied as GovernanceDenied
from parapetai_agent.governance_runtime import audit as _audit
from parapetai_agent.governance_runtime import configure_otel as configure_otel
from parapetai_agent.governance_runtime import (
    configure_rotating_audit_log as configure_rotating_audit_log,
)
from parapetai_agent.governance_runtime import (
    content_check_failure_decision as _content_check_failure_decision,
)
from parapetai_agent.governance_runtime import flush_otel as flush_otel
from parapetai_agent.governance_runtime import installed_version as _installed_version
from parapetai_agent.governance_runtime import otel_configured
from parapetai_agent.governance_runtime import record_tool_denial as _record_tool_denial
from parapetai_agent.governance_runtime import resolve_policy_source as _resolve_policy_source
from parapetai_agent.governance_runtime import set_oi_attributes as _set_oi_attributes
from parapetai_agent.governance_runtime import track_tool_denials as track_tool_denials
from parapetai_agent.governance_runtime import (
    unresolved_alter_decision as _unresolved_alter_decision,
)
from parapetai_agent.groundedness import GroundednessConfig
from parapetai_agent.identity import ANONYMOUS, Caller
from parapetai_agent.otel import openinference as oi
from parapetai_agent.policy.cost_tracker import CostTracker
from parapetai_agent.policy.cost_tracker import span_ids as _span_ids
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook
from parapetai_agent.policy.pricing import estimate_cost_usd_micros
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.response_judge import JudgeConfig
from parapetai_agent.scoped_data import (
    _CombinedIdentityContext,
    effective_identity_claims,
    effective_identity_roles,
)
from parapetai_agent.scoped_data import agent_identity as agent_identity
from parapetai_agent.scoped_data import current_identity as current_identity
from parapetai_agent.scoped_data import effective_principal as _effective_principal
from parapetai_agent.scoped_data import identity_from_bearer_token as identity_from_bearer_token
from parapetai_agent.scoped_data import set_current_identity as set_current_identity
from parapetai_agent.token_identity import TokenIdentityExtractor

log = structlog.get_logger(__name__)

# trace.get_tracer() returns a lazy proxy -- verified directly that calling
# it here, at import time, before configure_otel() (or anything) has set a
# real TracerProvider, still correctly picks up the real provider once
# configure_otel() calls trace.set_tracer_provider(), rather than being
# permanently bound to the no-op default. Until configure_otel() runs,
# spans are harmless no-ops (trace_id=0) -- importing this module stays
# side-effect free either way.
_tracer = trace.get_tracer(__name__)

# Maps a MAF chat client class name to the same provider strings the HTTP
# parsers use (parapetai_agent.providers.parsers.PARSERS), so one Cedar policy set
# governs both paths unmodified. Verified against real class names in
# agent_framework 1.13 (not guessed): agent_framework.openai.
# OpenAIChatClient/OpenAIChatCompletionClient, agent_framework.anthropic.
# AnthropicClient, agent_framework.gemini.GeminiChatClient,
# agent_framework.foundry.FoundryChatClient. Only openai has been exercised
# end-to-end against a live (fake) upstream through this module -- see
# docs/maf-in-process-integration.md for exactly what "supported" means per
# entry in parapetai-support.yaml. Unrecognised classes map to "unknown", never
# guessed.
#
# Azure OpenAI is NOT a distinct class: OpenAIChatClient/
# OpenAIChatCompletionClient serve both plain OpenAI and Azure OpenAI,
# differentiated only by construction (AZURE_OPENAI_ENDPOINT vs
# OPENAI_BASE_URL/OPENAI_API_KEY) -- verified live: constructing with
# AZURE_OPENAI_ENDPOINT set still reports type(client).__name__ ==
# "OpenAIChatCompletionClient", so class-name lookup alone is wrong for
# Azure. The client exposes `.azure_endpoint` (None for plain OpenAI, set for
# Azure) -- checked first, before the class-name table.
_PROVIDER_BY_CLIENT_CLASS = {
    "OpenAIChatClient": "openai",
    "OpenAIChatCompletionClient": "openai",
    "AnthropicClient": "anthropic",
    "GeminiChatClient": "gemini",
    "FoundryChatClient": "azure",
}


def provider_for_client(client: object) -> str:
    if getattr(client, "azure_endpoint", None):
        return "azure"
    return _PROVIDER_BY_CLIENT_CLASS.get(type(client).__name__, "unknown")


def _client_default_model(client: object) -> str | None:
    """Fallback for a real, verified gap: ChatContext.options only carries
    what the CALLER passed to this one get_response()/agent.run() call
    (agent_framework/_middleware.py's ChatMiddlewareLayer.get_response) --
    a client configured with model= only at construction (every Foundry
    example in this repo; also how an Azure-configured OpenAIChatClient/
    OpenAIChatCompletionClient is typically built) never puts "model" into
    per-call options at all. The client injects its OWN self.model deep
    inside its raw _prepare_options(), but only AFTER this middleware has
    already read context.options -- too late to observe from here.

    Verified directly against the installed agent_framework/
    agent_framework_openai/agent_framework_foundry/agent_framework_anthropic/
    agent_framework_gemini source: every one of those client classes stores
    its configured model as `self.model` -- not a Foundry-specific quirk.
    getattr(..., "model", None) is therefore a real, cross-provider fallback,
    not a guess, and never overrides an explicit per-call model (checked
    second, via `or`, at the call site). An unrecognised future client
    without a `.model` attribute degrades to None here exactly as it does
    today -- never an error, matching Snapshot.model's own optional-field
    contract."""
    return getattr(client, "model", None)


@dataclass(slots=True)
class _ChatCorrelation:
    """What ParapetChatMiddleware captured about the most recent model call,
    for ParapetFunctionMiddleware to read back when a tool call follows it.

    span_context: the model_call span's OTel SpanContext, so the following
    tool_call span can be explicitly linked as its child -- ChatMiddleware
    and FunctionMiddleware fire as sequential SIBLING calls from MAF's own
    run loop (verified, not assumed -- see module docstring), so ordinary
    ambient start_as_current_span() nesting would NOT produce a shared
    trace id on its own; by the time FunctionMiddleware runs, the
    model_call span has already exited its `with` block. A SpanContext
    (not the live Span) is what survives that gap -- see
    _parent_context_from_correlation().
    """

    provider: str = "unknown"
    model: str | None = None
    declared_tools: list[str] = field(default_factory=list)
    messages_preview: str = ""
    stream: bool = False
    span_context: SpanContext | None = None


_current_chat: contextvars.ContextVar[_ChatCorrelation | None] = contextvars.ContextVar(
    "parapetai_agent_maf_current_chat", default=None
)

_PREVIEW_CHARS = 2000

# COST-TRACK-1 (docs/adr/0010): cumulative token/cost totals for a
# model_call's trace and its own "span" (that model_call plus whatever
# tool_call(s) it triggers -- see the ADR for why that's the scope, not an
# arbitrary OTel subtree). One process-wide instance, same posture as
# _tracer above: trace_id is already globally unique, so sharing across
# every GovernedAgent/build_middleware() caller in this process is safe.
# span_ids() (trace_id/span_id as hex, or None for a no-op tracer's invalid
# context -- configure_otel() never called, see the trace.get_tracer()
# comment above) is shared with adk.py via policy/cost_tracker.py rather
# than each adapter formatting its own hex convention.
_cost_tracker = CostTracker()


def identity_from_azure_credential(
    credential: TokenCredential,
    scope: str = "https://cognitiveservices.azure.com/.default",
    *,
    extractor: TokenIdentityExtractor | None = None,
) -> _CombinedIdentityContext:
    """The same identity_from_bearer_token(), sourced from an
    azure-identity credential instead of a token you already have in
    hand -- e.g. the SAME AzureCliCredential passed to FoundryChatClient
    (examples/maf_sample_01/'s client=FoundryChatClient(credential=...)):

        credential = AzureCliCredential()
        client = FoundryChatClient(credential=credential)
        with identity_from_azure_credential(credential):
            result = await agent.run(...)

    Confirmed live: `az login`-issued Azure AD access tokens are real
    JWTs, and their standard claims (`oid`, `upn`/`preferred_username`,
    `tid`, `appid`, ...) are the same shape identity_from_bearer_token's
    default JwtIdentityExtractor already decodes -- no new parsing logic
    needed, this is a thin wrapper around get_token() + the existing
    decode path. `scope` only affects the token's `aud` claim (which
    resource it's valid for), not the identity claims this extracts --
    the default (Cognitive Services, what Foundry itself calls) works
    for any Azure AD-authenticated credential; override it if the
    embedding application already requests a specific scope elsewhere
    and wants to reuse that exact token instead of fetching a second one.

    SYNCHRONOUS credentials only (azure.identity.AzureCliCredential, the
    default). For an async one (azure.identity.aio.AzureCliCredential --
    see examples/maf_sample_06/, which uses it as an `async with`
    context manager), `await credential.get_token(scope)` yourself and
    pass `.token` straight to identity_from_bearer_token() instead --
    decoding a JWT is synchronous either way, only fetching one differs.
    """
    token = credential.get_token(scope).token
    return identity_from_bearer_token(token, extractor=extractor)


@contextlib.contextmanager
def governed_identity(
    *,
    claims: Mapping[str, Any] | None = None,
    roles: Sequence[Any] | None = None,
    token: str | None = None,
    credential: TokenCredential | None = None,
    scope: str = "https://cognitiveservices.azure.com/.default",
    extractor: TokenIdentityExtractor | None = None,
) -> Iterator[None]:
    """ONE context manager for every identity source this module knows
    how to read -- pick exactly one of (claims and/or roles), token, or
    credential, and the underlying mechanism (current_identity() /
    identity_from_bearer_token() / identity_from_azure_credential()) is
    chosen for you. Wrapping an agent.run() call in identity no longer
    means first deciding which of three functions matches the shape your
    identity data happens to be in -- one name, one call, regardless of
    source:

        # claims/roles already parsed
        with governed_identity(claims={"oid": "..."}, roles=["OrderViewer"]):
            await agent.run(query)

        # a raw bearer token
        with governed_identity(token=jwt):
            await agent.run(query)

        # an azure-identity credential (e.g. the SAME one passed to
        # FoundryChatClient -- see examples/maf_sample_01/)
        with governed_identity(credential=AzureCliCredential()):
            await agent.run(query)

    Fails LOUD, not silent, on ambiguity -- unlike an unwrapped
    agent.run(query), which evaluates against EMPTY identity_claims/
    identity_roles (Cedar's own default-deny already makes that the
    safe failure mode for any policy that checks identity: denied, not
    skipped or bypassed -- see _identity_claims's own docstring), a
    MISCONFIGURED call to this function raises immediately instead of
    quietly doing the wrong thing:
      - zero sources given: ValueError -- if there's genuinely no
        identity to assert, call agent.run() directly, unwrapped,
        rather than this function with nothing in it.
      - more than one source given: ValueError -- an author who passed
        both token= and credential= almost certainly left one in by
        mistake while editing, not intended "combine both somehow";
        there's no defined merge semantics across two different token
        sources to fall back on either.
    """
    has_claims_or_roles = claims is not None or roles is not None
    sources_given = sum([has_claims_or_roles, token is not None, credential is not None])
    if sources_given == 0:
        raise ValueError(
            "governed_identity() needs exactly one identity source (claims/roles, token, "
            "or credential) -- call agent.run() directly, unwrapped, if there's genuinely "
            "no identity to assert for this call"
        )
    if sources_given > 1:
        raise ValueError(
            "governed_identity() got more than one identity source -- pass exactly one of "
            "claims/roles, token, or credential, not a combination"
        )

    if credential is not None:
        with identity_from_azure_credential(credential, scope, extractor=extractor):
            yield
    elif token is not None:
        with identity_from_bearer_token(token, extractor=extractor):
            yield
    else:
        with current_identity(claims=claims, roles=roles):
            yield


def _identity_claims(kwargs: Mapping[str, Any] | None) -> dict[str, str]:
    """Explicit function_invocation_kwargs wins if present; otherwise falls
    back to whatever set_current_identity()/current_identity() set
    ambiently for the current asyncio task -- see
    scoped_data.effective_identity_claims() for the fallback semantics.
    Extracting the "identity_claims" key out of MAF's own
    function_invocation_kwargs shape is the one MAF-specific step; the
    explicit-wins-ambient-fallback precedence itself is shared with every
    other framework integration."""
    explicit = kwargs.get("identity_claims") if kwargs else None
    return effective_identity_claims(explicit if isinstance(explicit, dict) else None)


def _identity_roles(kwargs: Mapping[str, Any] | None) -> list[str]:
    """Same shape as _identity_claims, for the role SET -- see
    scoped_data.effective_identity_roles()."""
    explicit = kwargs.get("identity_roles") if kwargs else None
    return effective_identity_roles(explicit if isinstance(explicit, (list, tuple)) else None)


def _parent_context_from_correlation(chat: _ChatCorrelation) -> OtelContext | None:
    """Reconstructs a usable parent Context from a completed span's
    SpanContext alone (the live Span object is long gone by the time
    ParapetFunctionMiddleware runs -- see _ChatCorrelation.span_context's
    docstring). NonRecordingSpan is OTel's own mechanism for exactly this:
    a Span that carries a SpanContext for linking purposes without needing
    the original recording Span instance."""
    if chat.span_context is None:
        return None
    return trace.set_span_in_context(NonRecordingSpan(chat.span_context))


def _model_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {"_raw": str(value)[:4096]}


def _redact_all(value: Any) -> Any:
    """The one built-in ALTER transform -- a placeholder, not a real
    redaction strategy (this repo has no PII/secrets detector to ship).
    Registered under "redact_all" so tests and a first bundle have
    something real to name; a production deployment should register its
    own via build_middleware(alter_transforms=...), which OVERRIDES this
    entry for the same name rather than layering on top of it."""
    if isinstance(value, ChatResponse):
        return ChatResponse(messages=[Message("assistant", ["[REDACTED BY POLICY]"])])
    return "[REDACTED BY POLICY]"


DEFAULT_ALTER_TRANSFORMS: dict[str, Callable[[Any], Any]] = {"redact_all": _redact_all}


def _log_content_enabled() -> bool:
    """PARAPETAI_OTEL_LOG_CONTENT, default false -- opt-in gate for every
    OpenInference content_bearing attribute (full prompt/response/tool-arg
    text riding on a span), mirroring gateway/server/app.py's
    PARAPETAI_LOG_PROMPTS (ADR 0005) one layer over: that gates a structlog
    event, this gates span attributes. See docs/adr/0007. Read fresh on
    every call rather than cached at import time, so a test can
    monkeypatch it per-case the same way PARAPETAI_LOG_PROMPTS's own tests do."""
    return os.environ.get("PARAPETAI_OTEL_LOG_CONTENT", "false").lower() == "true"


def _token_count_attributes(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """llm.token_count.{prompt,completion,total} from agent_framework's
    ChatResponse.usage_details (a UsageDetails TypedDict --
    input_token_count/output_token_count/total_token_count, verified live
    against agent_framework._types.UsageDetails). Metadata-only (a count,
    never text), so callers set this unconditionally regardless of
    _log_content_enabled() -- a genuinely new capability, maf.py never
    read usage_details before this."""
    if not usage:
        return {}
    out: dict[str, int] = {}
    if usage.get("input_token_count") is not None:
        out[oi.LLM_TOKEN_COUNT_PROMPT] = usage["input_token_count"]
    if usage.get("output_token_count") is not None:
        out[oi.LLM_TOKEN_COUNT_COMPLETION] = usage["output_token_count"]
    if usage.get("total_token_count") is not None:
        out[oi.LLM_TOKEN_COUNT_TOTAL] = usage["total_token_count"]
    return out


# Name fragments that mark a tool as READ/retrieval. Only results from tools
# that match are usable as a groundedness SOURCE (see _grounding_source).
_READ_TOOL_HINTS = (
    "lookup",
    "get",
    "search",
    "find",
    "list",
    "fetch",
    "retrieve",
    "read",
    "query",
    "describe",
    "show",
    "view",
    "load",
    "select",
)


def _is_read_tool(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _READ_TOOL_HINTS)


def _grounding_source(messages: Any) -> str:
    """The SOURCE for the post-response groundedness check: the results of this
    turn's READ / retrieval tool calls -- what a faithful answer must be
    supported by. Returns "" when the turn produced no such source, so the
    caller SKIPS groundedness rather than flagging an answer against nothing.

    Critically, only READ results count. A WRITE action (create/update/delete)
    also returns a result, but its result is a confirmation, not a source to
    ground an answer against -- treating it as one false-denies legitimate
    write confirmations (a create_incident reply flagged as "ungrounded"). So a
    result is included ONLY when its tool is positively identified as a read
    (by name); unknown or write tools are excluded. Worst case groundedness
    under-runs; it never false-flags a write. Duck-typed over agent_framework
    message shapes: FunctionCallContent carries `.name`+`.call_id`,
    FunctionResultContent carries `.result` (+ `.call_id`/`.name`)."""
    # First pass: map function-call id -> tool name, so a result can be told
    # apart as read vs write even when the result content omits the name.
    call_names: dict[Any, str] = {}
    for m in messages or []:
        for c in getattr(m, "contents", None) or []:
            if getattr(c, "result", None) is not None:
                continue  # a result, not a call
            name = getattr(c, "name", None)
            cid = getattr(c, "call_id", None) or getattr(c, "id", None)
            if name and cid is not None:
                call_names[cid] = str(name)
    parts: list[str] = []
    for m in messages or []:
        for c in getattr(m, "contents", None) or []:
            result = getattr(c, "result", None)
            if result is None:
                continue
            cid = getattr(c, "call_id", None) or getattr(c, "id", None)
            name = str(getattr(c, "name", None) or call_names.get(cid, ""))
            if _is_read_tool(name):
                parts.append(str(result))
    return " ".join(p for p in parts if p).strip()


class ParapetChatMiddleware(ChatMiddleware):
    """Model-call governance -- the strong enforcement point (see module
    docstring: raising here is a real hard stop, verified against a canary
    upstream). Also populates the ContextVar ParapetFunctionMiddleware reads,
    so register both together via build_middleware() below.

    Runs TWO Cedar decisions per call, pre- and post-, via the shared
    parapetai_agent.policy.hooks.GovernanceHook: pre gates the outgoing request
    exactly as before this existed (unchanged, refactored onto the shared
    primitive); post evaluates the model's own response before it's let
    through, so a bundle can ALTER or DENY based on what the model actually
    said, not just what was asked of it. Streaming is the one place this
    is NOT a real gate: MAF only exposes a finalized-streaming-response
    hook (context.stream_result_hooks) AFTER every chunk has already
    reached the caller (verified against agent_framework's own
    ChatMiddlewarePipeline.execute() -- it wires stream_result_hooks onto
    the ResponseStream only once this method has already returned), so a
    streaming post-call decision can only audit what happened, never block
    or rewrite it. See docs/maf-in-process-integration.md."""

    def __init__(
        self,
        engine: PolicyEngine,
        caller: Caller,
        *,
        alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
        content_checks: ContentCheckConfig | None = None,
        groundedness: GroundednessConfig | None = None,
        judge: JudgeConfig | None = None,
    ) -> None:
        self.engine = engine
        self.caller = caller
        self.hook = GovernanceHook(engine, caller, on_decision=_audit)
        self._alter_transforms = {**DEFAULT_ALTER_TRANSFORMS, **(alter_transforms or {})}
        # Tier-2 content checks (parapetai_agent/content_checks.py) -- only run
        # on the PRE-call snapshot (messages_preview), mirroring tier 1's
        # own scope (control-plane's content_checks.py catalog entries are
        # model_call-only, gated on context.messages_preview). None means
        # "no tier-2 config" -- evaluate() below is then a harmless no-op,
        # same "safe when empty" shape ContentCheckConfig itself has.
        self._content_checks = content_checks
        # QUAL-1 groundedness -- runs on the POST-call response (the model's
        # own answer vs the source it was given), the mirror image of the
        # pre-call content checks above. Same fail-closed contract: a scanner
        # error is a hard deny before the post-stage Cedar runs.
        self._groundedness = groundedness
        # SLM judge -- also POST-call, an operator-defined rubric scored by a
        # small model (parapetai_agent/response_judge.py). Same fail-closed
        # contract as groundedness: a judge error is a hard deny before the
        # post-stage Cedar runs, so a missing verdict never reads as "passed".
        self._judge = judge

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        texts = [str(m.text) for m in context.messages if getattr(m, "text", None)]
        declared_tools = [
            t.name for t in (context.options or {}).get("tools", []) if hasattr(t, "name")
        ]
        # Wraps the whole method: this span stays "current" for the
        # duration of call_next() (the actual model call), and its
        # SpanContext is what ParapetFunctionMiddleware links its own
        # tool_call span to as a child -- see _ChatCorrelation.span_context.
        with _tracer.start_as_current_span("parapetai.model_call") as span:
            correlation = _ChatCorrelation(
                provider=provider_for_client(context.client),
                model=(context.options or {}).get("model") or _client_default_model(context.client),
                declared_tools=declared_tools,
                messages_preview=" ".join(texts)[:_PREVIEW_CHARS],
                stream=context.stream,
                span_context=span.get_span_context(),
            )
            _set_oi_attributes(
                span,
                {
                    oi.SPAN_KIND_ATTR: oi.SpanKind.LLM,
                    oi.LLM_MODEL_NAME: correlation.model,
                    oi.LLM_PROVIDER: correlation.provider,
                    oi.LLM_TOOLS: declared_tools,
                    oi.LLM_INVOCATION_PARAMETERS: json.dumps({"stream": context.stream}),
                },
            )
            if _log_content_enabled():
                # Full, pre-truncation message text -- `texts` above, not
                # correlation.messages_preview (capped at _PREVIEW_CHARS
                # for Cedar context matching). See docs/adr/0007: gated
                # behind PARAPETAI_OTEL_LOG_CONTENT, off by default.
                _set_oi_attributes(span, {oi.LLM_INPUT_MESSAGES: texts})
            # Deliberately NOT reset after this method returns (no
            # try/finally around a token) -- see the module docstring's
            # "_current_chat is not scoped to this method" section for why
            # that was a real, previously-shipped bug: FunctionMiddleware
            # fires as a SEPARATE, LATER call from MAF's own run loop,
            # after this method has already returned, so resetting here
            # made _current_chat.get() return None (the ContextVar's
            # default) by the time ParapetFunctionMiddleware ever read it
            # -- silently breaking provider/model correlation (every
            # tool_call decision showed context.provider == "unknown",
            # every time) and, now, span parenting too. Letting the value
            # persist until the NEXT model_call naturally overwrites it is
            # what actually matches MAF's real, sequential (not nested)
            # execution order.
            _current_chat.set(correlation)

            # ChatContext.kwargs and ChatContext.function_invocation_kwargs
            # are TWO SEPARATE attributes (verified by reading
            # agent_framework's own ChatContext.__init__, not assumed):
            # .kwargs comes from Agent.run(client_kwargs=...), while
            # Agent.run(function_invocation_kwargs=...) -- the documented way
            # to pass identity_claims/identity_roles into a run, see
            # build_middleware()'s docstring -- lands in
            # .function_invocation_kwargs instead. Reading .kwargs here was a
            # real bug: identity never reached a model_call Cedar decision in
            # any real Agent.run(), only in a test that built ChatContext by
            # hand. Caught by
            # test_identity_claims_flow_through_real_agent_run in
            # test_maf.py, which is exactly why that test exists.
            identity_claims = _identity_claims(context.function_invocation_kwargs)
            identity_roles = _identity_roles(context.function_invocation_kwargs)
            snapshot = Snapshot(
                provider=correlation.provider,
                endpoint="in-process:maf:model_call",
                model=correlation.model,
                parsed=True,
                messages_preview=correlation.messages_preview,
                declared_tools=correlation.declared_tools,
                stream=correlation.stream,
                identity_claims=identity_claims,
                identity_roles=identity_roles,
            )
            principal = _effective_principal(self.caller)
            # Tier-2 scanners run BEFORE the Cedar decision, never after --
            # see parapetai_agent.content_checks's module docstring for why a
            # scanner failure must deny here, before GovernanceHook.evaluate()
            # is even called, rather than let Cedar see an absent context key
            # and silently treat a configured check as a no-op.
            content_result = (
                self._content_checks.evaluate(snapshot) if self._content_checks else None
            )
            if content_result is not None and content_result.errors:
                denial = _content_check_failure_decision(
                    self.engine.status["generation"], content_result.errors
                )
                span.set_status(Status(StatusCode.ERROR, denial.reason))
                raise GovernanceDenied(denial)
            extra_context = content_result.context if content_result else {}
            # COST-TRACK-1: cumulative totals so far (NOT including this call --
            # its own usage isn't known until the response comes back below).
            # scope_id is this model_call's OWN span id: the "span" scope for
            # this whole turn (this call plus any tool_call(s) it goes on to
            # trigger, correlated via `correlation.span_context` below) is
            # keyed off of it, not invented as a separate id.
            span_ids = _span_ids(span.get_span_context())
            trace_id, scope_id = span_ids if span_ids else (None, None)
            extra_context = {
                **extra_context,
                **_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
            }
            pre = self.hook.evaluate(
                snapshot=snapshot, stage="pre", principal=principal, extra_context=extra_context
            )
            if not pre.decision.allowed:
                span.set_status(Status(StatusCode.ERROR, pre.decision.reason))
                raise GovernanceDenied(pre.decision)

            await call_next()

            if context.stream:
                # Can only audit -- see class docstring and
                # _register_stream_audit_hook's own docstring for why this
                # can never block or rewrite what already streamed.
                self._register_stream_audit_hook(
                    context,
                    principal,
                    correlation,
                    identity_claims,
                    identity_roles,
                    trace_id,
                    scope_id,
                )
                return

            chat_response = context.result
            if not isinstance(chat_response, ChatResponse):
                return  # an earlier middleware already overrode/denied -- nothing of ours to check
            token_attrs = _token_count_attributes(chat_response.usage_details)
            _set_oi_attributes(span, token_attrs)
            # COST-TRACK-1: this call's usage is known now (it wasn't at the
            # pre-call context_for() above) -- record it against the SAME
            # (trace_id, scope_id) so the next call in this trace/turn sees
            # an up-to-date cumulative total. A model with no priced rate
            # still credits tokens (real, known) while cost_usd_micros stays
            # 0 for it -- see pricing.py's estimate_cost_usd_micros docstring
            # for why that's None-coalesced to 0 here rather than skipped:
            # an unpriced model's token spend should still count toward a
            # TOKEN budget even though it can't contribute to a DOLLAR one.
            if trace_id is not None:
                prompt_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_PROMPT, 0)
                completion_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_COMPLETION, 0)
                total_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_TOTAL, 0) or (
                    prompt_tok + completion_tok
                )
                cost_micros = (
                    estimate_cost_usd_micros(correlation.model, prompt_tok, completion_tok) or 0
                )
                _cost_tracker.record(
                    trace_id=trace_id,
                    scope_id=scope_id,
                    tokens=total_tok,
                    cost_usd_micros=cost_micros,
                )
            if _log_content_enabled():
                _set_oi_attributes(span, {oi.LLM_OUTPUT_MESSAGES: chat_response.text})
            response_snapshot = Snapshot(
                provider=correlation.provider,
                endpoint="in-process:maf:model_call",
                model=correlation.model,
                parsed=True,
                response_preview=chat_response.text[:_PREVIEW_CHARS],
                identity_claims=identity_claims,
                identity_roles=identity_roles,
            )
            # QUAL-1: score the model's own response against the source it was
            # given (the prompt), in-process. Mirrors the PRE-call tier-2 path:
            # a scanner error is a HARD DENY here, before the post-stage Cedar
            # is ever evaluated, so an absent groundedness key can never read as
            # "grounded". The verdict is a flat bool merged into the post
            # context; the response/source text never leaves this process.
            # COST-TRACK-1: seeded with the cumulative totals AFTER this
            # call's own usage was just recorded above, so a post-stage
            # policy (e.g. ALTER once a turn crosses a token budget) sees
            # the up-to-date picture, not the pre-call snapshot.
            post_context: dict[str, Any] = _cost_tracker.context_for(
                trace_id=trace_id, scope_id=scope_id
            )
            if self._groundedness is not None and self._groundedness.active:
                # FALSE-POSITIVE GUARD. Groundedness asks "is this answer
                # supported by its SOURCE?" -- and the source is the retrieval /
                # tool result the answer is meant to be faithful to, NOT the
                # user's prompt. Scoring a tool-derived answer against the bare
                # prompt flags every useful answer as "ungrounded" (the source
                # never contained the tool's data). So build the source from
                # THIS turn's grounding material (function/tool results) and run
                # the check ONLY when one exists; with no source, groundedness is
                # undefined and is skipped (allowed), never a false deny.
                grounding_source = _grounding_source(context.messages)
                if grounding_source:
                    g_result = self._groundedness.evaluate_post(
                        chat_response.text, grounding_source
                    )
                    if g_result.errors:
                        denial = _content_check_failure_decision(
                            self.engine.status["generation"], g_result.errors
                        )
                        span.set_status(Status(StatusCode.ERROR, denial.reason))
                        raise GovernanceDenied(denial)
                    post_context.update(g_result.context)
            # SLM judge verdict merges into the SAME post context, so one
            # post-stage Cedar decision sees both groundedness and judge keys.
            if self._judge is not None and self._judge.active:
                j_result = self._judge.evaluate_post(chat_response.text)
                if j_result.errors:
                    denial = _content_check_failure_decision(
                        self.engine.status["generation"], j_result.errors
                    )
                    span.set_status(Status(StatusCode.ERROR, denial.reason))
                    raise GovernanceDenied(denial)
                post_context.update(j_result.context)
            post = self.hook.evaluate(
                snapshot=response_snapshot,
                stage="post",
                principal=principal,
                extra_context=post_context or None,
            )
            if not post.decision.allowed:
                span.set_status(Status(StatusCode.ERROR, post.decision.reason))
                raise GovernanceDenied(post.decision)
            if post.alter_with is not None:
                transform = self._alter_transforms.get(post.alter_with)
                if transform is None:
                    denial = _unresolved_alter_decision(
                        post.decision.policy_generation, post.alter_with
                    )
                    span.set_status(Status(StatusCode.ERROR, denial.reason))
                    raise GovernanceDenied(denial)
                context.result = transform(chat_response)

    def _register_stream_audit_hook(
        self,
        context: ChatContext,
        principal: str,
        correlation: _ChatCorrelation,
        identity_claims: dict[str, str],
        identity_roles: list[str],
        trace_id: str | None,
        scope_id: str | None,
    ) -> None:
        """MAF only wires context.stream_result_hooks onto the returned
        ResponseStream AFTER this middleware's process() has fully
        returned (agent_framework's ChatMiddlewarePipeline.execute():
        `if context.result and isinstance(context.result, ResponseStream):
        ... context.result.with_result_hook(...)` runs only once
        `await first_handler()` -- the whole middleware chain, us included
        -- has unwound). And that hook itself only fires once the stream
        is fully finalized, which requires the CALLER to have already
        consumed every chunk. So by the time this hook's body runs, there
        is nothing left to block or rewrite -- it can only ever audit what
        was already delivered. Registered here (append, not call) so it's
        in place before process() returns and MAF wires it up; running the
        actual evaluation is deferred to when it fires, since the
        finalized ChatResponse doesn't exist yet at this point.

        Same reason this doesn't attempt span.set_status() on a deny/alter
        (see the log.warning calls below instead of raising): the
        "parapetai.model_call" span itself has already ended by the time this
        fires -- the `with _tracer.start_as_current_span(...)` block that
        opened it exited when process() returned, well before the caller
        finishes consuming the stream. is_recording() on an ended span is
        False, so _set_oi_attributes would be a silent no-op here -- token
        counts/output_messages for a STREAMING call are therefore not
        attached to a span at all, a real, documented gap versus the
        non-streaming path, not an oversight.

        COST-TRACK-1 is NOT subject to that gap: trace_id/scope_id are
        SpanContext data captured before the span ended (they don't need a
        still-recording span, unlike _set_oi_attributes), and `finalized`
        below carries its own usage_details same as the non-streaming
        ChatResponse -- so cumulative cost tracking stays accurate across a
        streaming call even though its span never gets token-count
        attributes."""

        async def _audit_finalized(finalized: ChatResponse) -> ChatResponse:
            if trace_id is not None:
                token_attrs = _token_count_attributes(finalized.usage_details)
                prompt_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_PROMPT, 0)
                completion_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_COMPLETION, 0)
                total_tok = token_attrs.get(oi.LLM_TOKEN_COUNT_TOTAL, 0) or (
                    prompt_tok + completion_tok
                )
                cost_micros = (
                    estimate_cost_usd_micros(correlation.model, prompt_tok, completion_tok) or 0
                )
                _cost_tracker.record(
                    trace_id=trace_id,
                    scope_id=scope_id,
                    tokens=total_tok,
                    cost_usd_micros=cost_micros,
                )
            response_snapshot = Snapshot(
                provider=correlation.provider,
                endpoint="in-process:maf:model_call",
                model=correlation.model,
                parsed=True,
                stream=True,
                response_preview=finalized.text[:_PREVIEW_CHARS],
                identity_claims=identity_claims,
                identity_roles=identity_roles,
            )
            result = self.hook.evaluate(
                snapshot=response_snapshot, stage="post", principal=principal
            )
            if not result.decision.allowed:
                log.warning(
                    "post_call_would_deny_streaming",
                    reason=result.decision.reason,
                    principal=principal,
                )
            elif result.alter_with is not None:
                log.warning(
                    "post_call_would_alter_streaming",
                    alter_with=result.alter_with,
                    principal=principal,
                )
            return finalized

        context.stream_result_hooks = [*context.stream_result_hooks, _audit_finalized]


class ParapetFunctionMiddleware(FunctionMiddleware):
    """Tool-call governance -- native and MCP-sourced alike (see
    spike/maf_mcp_check/: both hit this same hook, same additional_properties
    shape distinguishes them). On deny: NOT calling call_next() reliably
    prevents the underlying code/RPC from running (verified), but the denial
    itself is communicated as a context.result string, not a raised
    exception -- see module docstring for why raising here doesn't work.

    Post-call ALTER/DENY here has none of ParapetChatMiddleware's
    streaming caveat -- a tool result is never streamed, so a post-call
    decision genuinely gates what reaches the model, the same way the
    pre-call one always has."""

    def __init__(
        self,
        engine: PolicyEngine,
        caller: Caller,
        *,
        alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
    ) -> None:
        self.engine = engine
        self.caller = caller
        self.hook = GovernanceHook(engine, caller, on_decision=_audit)
        self._alter_transforms = {**DEFAULT_ALTER_TRANSFORMS, **(alter_transforms or {})}

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        chat = _current_chat.get() or _ChatCorrelation()
        # Explicitly linked to the triggering model_call span via its
        # SpanContext, not ambient nesting -- see _ChatCorrelation's
        # docstring for why ambient nesting alone would not work here
        # (ChatMiddleware and FunctionMiddleware fire as sequential
        # siblings, not one inside the other).
        with _tracer.start_as_current_span(
            "parapetai.tool_call", context=_parent_context_from_correlation(chat)
        ) as span:
            identity_claims = _identity_claims(context.kwargs)
            identity_roles = _identity_roles(context.kwargs)
            snapshot = Snapshot(
                provider=chat.provider,
                endpoint="in-process:maf:tool_call",
                model=chat.model,
                parsed=True,
                tool_name=context.function.name,
                tool_args=_model_to_dict(context.arguments),
                identity_claims=identity_claims,
                identity_roles=identity_roles,
            )
            _set_oi_attributes(
                span,
                {oi.SPAN_KIND_ATTR: oi.SpanKind.TOOL, oi.TOOL_NAME: context.function.name},
            )
            if _log_content_enabled():
                # Full argument dict, not a preview -- same _model_to_dict()
                # coercion tool_args above already used, so this is the
                # exact data Cedar's own tool_args context key saw.
                _set_oi_attributes(
                    span, {oi.TOOL_PARAMETERS: json.dumps(snapshot.tool_args, default=str)}
                )
            principal = _effective_principal(self.caller)
            # COST-TRACK-1: scope_id is the TRIGGERING model_call's own span
            # id (via `chat`, the SAME correlation that links this tool_call
            # span to it as a child -- see the docstring above) so a tool
            # call accumulates into the SAME "turn" total as the model_call
            # that requested it, not a scope of its own. Falls back to this
            # span's own id only for a tool_call with no correlated
            # model_call at all (chat is a fresh default _ChatCorrelation()).
            own_ids = _span_ids(span.get_span_context())
            trace_id, own_span_id = own_ids if own_ids else (None, None)
            correlated_ids = _span_ids(chat.span_context) if chat.span_context else None
            scope_id = correlated_ids[1] if correlated_ids else own_span_id
            cost_context = _cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id)
            pre = self.hook.evaluate(
                snapshot=snapshot, stage="pre", principal=principal, extra_context=cost_context
            )
            if not pre.decision.allowed:
                span.set_status(Status(StatusCode.ERROR, pre.decision.reason))
                _record_tool_denial(pre.decision.reason)
                context.result = f"GOVERNANCE_DENIED: {pre.decision.reason}"
                return

            await call_next()

            if _log_content_enabled():
                _set_oi_attributes(span, {oi.OUTPUT_VALUE: str(context.result)})
            response_snapshot = Snapshot(
                provider=chat.provider,
                endpoint="in-process:maf:tool_call",
                model=chat.model,
                parsed=True,
                tool_name=context.function.name,
                tool_result_preview=str(context.result)[:_PREVIEW_CHARS],
                identity_claims=identity_claims,
                identity_roles=identity_roles,
            )
            # Tool calls consume no LLM tokens of their own, so nothing is
            # recorded here (record() is a model_call-only concern) -- but
            # the cumulative totals a tool_call's own DENY/ALTER decision
            # reasons about should still reflect the model_call that just
            # preceded it, hence re-reading (not re-using the pre-call
            # value, which is already stale by the time call_next() returns).
            post = self.hook.evaluate(
                snapshot=response_snapshot,
                stage="post",
                principal=principal,
                extra_context=_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
            )
            if not post.decision.allowed:
                span.set_status(Status(StatusCode.ERROR, post.decision.reason))
                _record_tool_denial(post.decision.reason)
                context.result = f"GOVERNANCE_DENIED: {post.decision.reason}"
                return
            if post.alter_with is not None:
                transform = self._alter_transforms.get(post.alter_with)
                if transform is None:
                    denial = _unresolved_alter_decision(
                        post.decision.policy_generation, post.alter_with
                    )
                    span.set_status(Status(StatusCode.ERROR, denial.reason))
                    _record_tool_denial(denial.reason)
                    context.result = f"GOVERNANCE_DENIED: {denial.reason}"
                    return
                context.result = transform(context.result)


@dataclass(slots=True)
class _MiddlewareRegistryEntry:
    engine: PolicyEngine
    chat_mw: ParapetChatMiddleware
    func_mw: ParapetFunctionMiddleware
    stop_event: threading.Event | None  # None when no background sync was started
    thread: threading.Thread | None = None  # the background poller, so reset can join it


_middleware_registry: dict[tuple[str, str, str, str, str], _MiddlewareRegistryEntry] = {}
_middleware_registry_lock = threading.Lock()


def reset_middleware_registry() -> None:
    """Test-only: stops every background sync thread build_middleware() has
    started in this process and forgets all cached middleware, so the next
    build_middleware() call for a previously-seen identity does real
    construction again instead of returning a stale, previous test's
    engine/middleware. Real callers never need this -- the registry is
    supposed to outlive the whole process.

    Signalling stop_event alone is not enough: a poller mid-cycle finishes its
    current poll_once + heartbeat AFTER the signal and BEFORE it next checks the
    event, so that stray request lands in whichever test runs next (a bundle GET
    that inflates a call-count assertion, or a heartbeat to a URL only the prior
    test mocked -- 'RESPX: ... not mocked!'). So we also JOIN each poller here,
    with a bounded timeout, so no background thread outlives the reset."""
    with _middleware_registry_lock:
        entries = list(_middleware_registry.values())
        _middleware_registry.clear()
    for entry in entries:
        if entry.stop_event is not None:
            entry.stop_event.set()
    for entry in entries:
        if entry.thread is not None:
            entry.thread.join(timeout=5.0)


def build_middleware(
    policy_dir: str | Path | None = None,
    entities_path: str | Path | None = None,
    agent_id: str | None = None,
    tenant: str = "default",
    control_plane_url: str | None = None,
    agent_secret: str | None = None,
    pep_key_path: str | Path | None = None,
    persist_policy_dir: str | Path | None = None,
    local_log_dir: str | Path | None = None,
    persist_pep_key: bool = True,
    otel_log_mode: Literal["streaming", "buffered"] = "buffered",
    console: bool = True,
    alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
) -> tuple[ParapetChatMiddleware, ParapetFunctionMiddleware]:
    """One PolicyEngine, one Caller, both middleware -- the pairing this
    module is designed around; register both on the same Agent so
    tool-call decisions can see which model call preceded them.

    policy_dir/entities_path are now BOTH optional -- see
    _resolve_policy_source()'s own docstring for the bundled-default
    fallback this enables. Pass a real policy_dir only when you have your
    own Cedar policies to enforce locally (no control plane) or want to
    override the bundled starting point.

    local_log_dir, when given, calls configure_rotating_audit_log()
    internally (idempotent per directory -- see that function's own
    docstring) -- one less required call at the embedding application's
    own top level. Omitted: no local rotating file, same as never calling
    configure_rotating_audit_log() at all (structlog's own console output
    still happens regardless -- this only controls the OPTIONAL file
    sink).

    OpenTelemetry is wired up automatically too, on the same condition as
    the control-plane bundle pull below: once control_plane_url/
    agent_secret both resolve, this calls configure_otel() FOR you --
    otlp_endpoint defaults to PARAPETAI_OTLP_ENDPOINT if set, else the same
    resolved control_plane_url (same host, standard OTLP/HTTP paths --
    see configure_otel()'s own docstring), otlp_headers carries the
    resolved agent_secret as a bearer token. Real bug, found live
    building examples/maf_webapp/: Cedar enforcement worked fine with a
    control plane configured but no configure_otel() call, and every
    decision produced ZERO spans/logs anywhere outside the local
    structlog console/local_log_dir -- this closes that gap by
    construction instead of relying on every embedder remembering a
    second setup call, the same reasoning GovernedAgent itself exists
    for (see its own docstring). otel_log_mode/console (below) are the
    two knobs this auto-wiring exposes -- otel_log_mode passed straight
    through to configure_otel()'s own log_mode=, console passed straight
    through to its own console= (see console's own paragraph below --
    ONE flag covers both this AND the local_log_dir stream). Anything
    past that (a custom service_name, Azure Monitor export, batch
    tuning) needs configure_otel() called explicitly -- and if the
    embedding application already called it BEFORE this function runs
    (checked via module state, not a flag), this auto-wiring steps aside
    entirely and leaves that configuration alone: whichever
    configure_otel() call happens first wins, since OTel's own
    TracerProvider/LoggerProvider registration is process-wide and
    set-once. See examples/maf_webapp/web_app.py's lifespan() for
    exactly that pattern (its own configure_otel() call, for Azure
    Monitor export, runs before build_middleware()'s priming call).
    Only fires when control_plane_url/agent_secret resolve -- with no
    control plane there is nowhere to ship spans/logs to, so nothing is
    auto-configured (local_log_dir's file sink is unaffected either
    way).

    console (default True) governs BOTH console outputs uniformly --
    local_log_dir's own stream_handler (see configure_rotating_audit_log()'s
    own console= param) AND the auto-wired configure_otel() call's own
    console= above. False suppresses both: no structlog JSON lines, no
    raw OTel span/LogRecord dump, printed to stdout -- the file sink
    (local_log_dir) and the shipped-to-control-plane telemetry (OTel,
    once configured) are UNAFFECTED either way; this only controls what
    prints locally, for a CLI example whose own printed output
    (`print(f"Agent: {result}")`) shouldn't be interleaved with a JSON
    decision stream. See examples/maf_sample_01/'s own module docstring
    for exactly that case.

    `agent_id`/`tenant` play the role resolve_from_path() plays for HTTP
    traffic, but there is no path to parse in-process: identity here is
    whatever the embedding application already knows about itself.
    `agent_id` is OPTIONAL, deliberately mirroring the HTTP path's own
    fallback (parapetai_agent.identity.resolve_from_path: no prefix -> Agent::
    "anonymous", still evaluated by Cedar's default-deny, never a bypass)
    rather than inventing a different unauthenticated-agent convention
    here -- omit it if a real Service Principal identity will arrive later
    via a token (identity_from_bearer_token()/set_current_agent_identity(),
    which override this Caller's static agent_id for any decision made
    while set -- see _effective_principal()).

    Fallback order when `agent_id` is not passed explicitly: the
    PARAPETAI_AGENT_ID environment variable, then ANONYMOUS -- the same
    "decoupled from code, settable via command line or env file" story
    parapetai_gateway.server.main's CLI flags give the HTTP gateway path, so an
    in-process embedder can provision an agent via the control plane's web
    UI and wire it in purely through the environment, no code change.

    control_plane_url/agent_secret (or their PARAPETAI_CONTROL_PLANE_URL/
    PARAPETAI_AGENT_SECRET env fallbacks) are OPT-IN, same as the HTTP
    gateway's own bundle pull (parapetai_gateway.server.main). When both resolve, the
    named agent's real bundle REPLACES whatever policy_dir resolved to
    (the bundled default, if policy_dir was omitted) -- disk persistence
    of that pulled bundle is its OWN opt-in, via persist_policy_dir:

      - persist_policy_dir given: a SYNCHRONOUS fetch-and-write-to-
        persist_policy_dir happens first, then PolicyEngine reads from
        there -- the ORIGINAL behavior (persist_policy_dir plays the
        role policy_dir used to play as a write target), for anything
        that wants a real on-disk cache surviving a restart with the
        control plane briefly unreachable. Fails closed (raises) if that
        first fetch fails with nothing on disk yet at persist_policy_dir.
      - persist_policy_dir omitted (the default): PolicyEngine is
        constructed FIRST from policy_dir (the bundled default unless
        overridden), so it's already enforcing something real, then the
        real bundle is fetched and applied directly to that engine's
        memory (PolicyEngine.load_from_bundle(), no disk write at all --
        parapetai_agent.control_plane.poll_once's persist_to_disk=False).
        If that first fetch fails (control plane unreachable), the
        engine keeps serving policy_dir's policies rather than crashing
        the whole process -- deliberately more resilient than the
        disk-persisted path for a serverless/k8s cold start, where
        crashing on a transient control-plane hiccup is worse than
        briefly enforcing a known-safe baseline until the next poll
        succeeds. Logged either way, never silent.

    Either way, this then starts ONE background thread that keeps the
    engine synced AND heartbeats on one interval
    (parapetai_agent.control_plane.run_bundle_poller, engine=... so it applies
    fetched bundles directly to memory -- no separate file-watcher needed
    for this path -- persist_to_disk threaded through to match whichever
    branch above ran).

    Also generates/loads this PEP's Ed25519 keypair and registers its
    public key with the control plane BEFORE that first fetch
    (parapetai_agent.control_plane.ensure_pep_identity), then signs every
    bundle-pull/heartbeat this identity makes from then on -- see
    control-plane/src/parapetai_control/keys.py for the verification side.
    pep_key_path overrides where that keypair is stored/loaded from
    (default: PARAPETAI_PEP_KEY_PATH env var, else ~/.parapetai/pep_ed25519.key)
    -- pass an explicit path for test isolation or to run multiple
    distinct PEP identities from one host. persist_pep_key=False (default
    True) skips disk for this too -- a fresh, never-written keypair every
    call, for a process with no writable filesystem at all; pep_key_path
    is ignored in that case. Trades identity STABILITY across restarts
    (a control-plane-initiated rotation becomes a no-op -- there's
    nothing durable to rotate) for that, not correctness -- see
    parapetai_agent.pep_identity.generate_ephemeral_keypair()'s own docstring
    for the full tradeoff and when a real (even ephemeral-per-cold-start)
    path is still preferable.

    IDEMPOTENT, by (resolved policy_dir, resolved entities_path, agent_id,
    tenant, control_plane_url): call this once, or a thousand times, for
    the same resolved identity, and after the first call every subsequent
    one returns the SAME PolicyEngine/middleware pair and starts no new
    thread -- verified live, not assumed, that a naive design here (a
    fresh background thread every call) breaks any caller that
    legitimately calls build_middleware() more than once for one identity,
    e.g. a web server sharing one governed agent across many chat
    sessions. A DIFFERENT key (different agent_id, different policy_dir,
    ...) always gets its own independent engine/middleware/thread, as it
    should -- this is per-identity reuse, not a single global singleton.
    reset_middleware_registry() (test-only) clears it and stops every
    thread it started. persist_policy_dir/local_log_dir are NOT part of
    the key -- they affect how construction happens, not what identity
    this is.

    Layer verified per-call end-user identity on top either explicitly, via
    function_invocation_kwargs={"identity_claims": {...}, "identity_roles":
    [...]} on agent.run(), or ambiently, via
    current_identity(claims=..., roles=...) / set_current_identity() so it
    doesn't need repeating on every call -- see _identity_claims,
    current_identity(), and docs/maf-in-process-integration.md. Most
    callers should use GovernedAgent (below) instead of calling this
    directly.

    alter_transforms: named callables a post-call ALTER decision (a bundle
    permit carrying @action("alter") + @alter_with("<name>")) applies to a
    model response or tool result before it's let through -- see
    ParapetChatMiddleware/ParapetFunctionMiddleware's own docstrings.
    Merged OVER DEFAULT_ALTER_TRANSFORMS (currently just "redact_all", a
    placeholder, not a real redaction strategy), so passing
    {"redact_all": my_fn} replaces the built-in rather than requiring it be
    kept. A bundle naming a transform that isn't registered here fails
    closed to a deny, per invariant 1 -- never a silent pass-through of the
    original, unaltered content. NOT part of the identity key below, same
    as local_log_dir/persist_policy_dir -- affects construction, not
    identity; the first build_middleware() call for a given identity's
    registry wins for every later call that reuses the cached middleware.
    """
    if local_log_dir is not None:
        configure_rotating_audit_log(local_log_dir, console=console)

    resolved_control_plane_url = control_plane_url or os.environ.get("PARAPETAI_CONTROL_PLANE_URL")
    resolved_agent_secret = agent_secret or os.environ.get("PARAPETAI_AGENT_SECRET")
    control_plane_configured = bool(resolved_control_plane_url and resolved_agent_secret)

    if control_plane_configured and not otel_configured():
        # See this function's own docstring's "OpenTelemetry is wired up
        # automatically too" paragraph. otel_configured() is a shared,
        # process-wide check (parapetai_agent.governance_runtime) so this
        # stays idempotent AND yields to an embedder's own, earlier
        # configure_otel() call (e.g. for Azure Monitor export) -- OR to
        # another framework integration's build_*() call in the same
        # process (e.g. parapetai_agent.adk.build_plugin()) -- first call
        # wins, matching OTel's own set-once global registration.
        assert resolved_control_plane_url and resolved_agent_secret  # narrows for mypy
        configure_otel(
            otlp_endpoint=os.environ.get("PARAPETAI_OTLP_ENDPOINT") or resolved_control_plane_url,
            otlp_headers={"Authorization": f"Bearer {resolved_agent_secret}"},
            log_mode=otel_log_mode,
            console=console,
        )

    resolved_agent_id = agent_id or os.environ.get("PARAPETAI_AGENT_ID") or ANONYMOUS
    resolved_policy_dir, resolved_entities_path = _resolve_policy_source(policy_dir, entities_path)

    key = (
        str(resolved_policy_dir.resolve()),
        str(resolved_entities_path.resolve()) if resolved_entities_path else "",
        resolved_agent_id,
        tenant,
        resolved_control_plane_url or "",
    )

    with _middleware_registry_lock:
        cached = _middleware_registry.get(key)
        if cached is not None:
            return cached.chat_mw, cached.func_mw

        resolved_pep_key_path = (
            (Path(pep_key_path) if pep_key_path else pep_identity.default_key_path())
            if persist_pep_key
            else None
        )
        # Tier-2 content-check scanner config -- ALWAYS constructed, never a
        # caller-supplied opt-in. If it were an optional parameter, the
        # control plane could enforce a tier-2 selection for an agent whose
        # SDK build simply forgot to pass one; unconditional construction
        # means any SDK version new enough to have this module at all
        # enforces whatever content_checks.json its bundle carries, with no
        # separate flag to remember. Populated below via poll_once()'s
        # generic on_bundle hook, same call sites/timing as `engine` --
        # empty (harmless no-op) until the first successful fetch.
        content_checks = ContentCheckConfig()
        groundedness = GroundednessConfig()
        judge = JudgeConfig()

        def _load_bundle_configs(files: dict[str, str]) -> None:
            # All SDK-side configs refresh from the SAME bundle on every fetch
            # (poll_once / run_bundle_poller call this via on_bundle) -- content
            # checks (pre), groundedness and the SLM judge (post) stay in
            # lockstep with policy.
            content_checks.load_from_bundle(files)
            groundedness.load_from_bundle(files)
            judge.load_from_bundle(files)

        stop_event: threading.Event | None = None
        poll_thread: threading.Thread | None = None
        if control_plane_configured:
            assert resolved_control_plane_url and resolved_agent_secret  # narrows for mypy
            # Identity registration, first fetch, the disk-vs-memory choice,
            # heartbeat and poller thread all live in ONE place
            # (parapetai_agent.control_plane.bootstrap_engine), shared by this
            # adapter, adk.py, and Governor.from_control_plane(). It was three
            # near-identical copies; three copies means three sets of outage
            # semantics, so "the agent acts as configured" could mean
            # something different depending on which integration a developer
            # picked. Behaviour is unchanged -- see that function's docstring
            # for the persist_policy_dir / in-memory split this used to spell
            # out inline.
            boot = bootstrap_engine(
                resolved_control_plane_url,
                resolved_agent_secret,
                policy_dir=resolved_policy_dir,
                entities_path=resolved_entities_path,
                persist_policy_dir=persist_policy_dir,
                pep_key_path=resolved_pep_key_path,
                mode="enforce",  # always enforces; no monitor-only mode here
                version=_installed_version(),
                poller_name=f"bundle-poll-{resolved_agent_id}",
                on_bundle=_load_bundle_configs,
            )
            engine = boot.engine
            stop_event = boot.stop_event
            poll_thread = boot.thread
        else:
            engine = PolicyEngine(resolved_policy_dir, resolved_entities_path)

        caller = Caller(agent_id=resolved_agent_id, tenant=tenant)
        chat_mw = ParapetChatMiddleware(
            engine,
            caller,
            alter_transforms=alter_transforms,
            content_checks=content_checks,
            groundedness=groundedness,
            judge=judge,
        )
        func_mw = ParapetFunctionMiddleware(engine, caller, alter_transforms=alter_transforms)
        _middleware_registry[key] = _MiddlewareRegistryEntry(
            engine, chat_mw, func_mw, stop_event, poll_thread
        )
        return chat_mw, func_mw


class GovernedAgent(Agent):
    """agent_framework.Agent with Cedar governance wired in automatically --
    a drop-in replacement for Agent(...) that removes the one real gap in
    this module's design: build_middleware() + middleware=[chat_mw, func_mw]
    is genuinely the ENTIRE integration (three lines), but it's opt-in per
    Agent(...) call site. Forget the middleware= kwarg and there is zero
    enforcement, silently -- Cedar's own default-deny only ever applies
    once a decision is actually evaluated, and nothing forces that to
    happen. Swapping the Agent import for GovernedAgent removes that
    failure mode: governance is constructed and attached inside __init__,
    not left to be remembered at every call site.

    This is deliberately NOT a process-wide monkeypatch of
    agent_framework.Agent.__init__ (the other option discussed, and
    rejected here) -- that would make EVERY Agent(...) anywhere in a
    process governed with no visible change at the call site, which is a
    stronger "default" but trades away exactly the kind of explicitness
    this whole codebase otherwise insists on (verify, don't assume; no
    hidden behaviour). GovernedAgent still requires touching each call
    site -- swap the import -- but that's a single, visible, one-line
    change instead of three easy-to-forget ones.

    Any middleware passed explicitly via middleware=[...] runs AFTER the
    governance middleware (Cedar decides first), not instead of it.

    agent_id is optional -- see build_middleware()'s docstring for the
    Agent::"anonymous" fallback and how a real Service Principal identity
    from a token can override it later via identity_from_bearer_token().

    EVERYTHING below is optional -- the minimal call is
    `GovernedAgent(client=..., name=..., instructions=...)`, nothing
    more, and it enforces real (if generic) Cedar policy from the moment
    it's constructed, using the policy set bundled in parapetai-agent (see
    build_middleware()'s own docstring / _resolve_policy_source()).
    policy_dir/entities_path override that bundled default with your own
    local Cedar policies. control_plane_url/agent_secret/pep_key_path
    (or their PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET env fallbacks)
    are passed straight through to build_middleware() -- see its
    docstring for the opt-in startup pull this triggers (same behavior
    the standalone gateway has at boot), the Ed25519 identity it
    registers, and exactly what persist_policy_dir/local_log_dir/
    persist_pep_key do -- all three exist for the SAME reason: a process
    with no writable filesystem at all (serverless, a read-only
    container) still needs a working default for each.

    OpenTelemetry is likewise automatic once control_plane_url/
    agent_secret resolve -- build_middleware() calls configure_otel()
    for you (see its own docstring's "OpenTelemetry is wired up
    automatically too" paragraph); no separate configure_otel() call is
    required to see Cedar decisions show up in the control plane's OTel
    log table. otel_log_mode ("buffered", the default, or "streaming")
    is one of the two knobs exposed here -- anything beyond that (Azure
    Monitor export, a custom service_name, batch tuning) still needs an
    explicit, earlier configure_otel() call, which this auto-wiring
    detects and steps aside for.

    console (default True) governs both console outputs uniformly --
    local_log_dir's own stream to stdout AND the auto-wired
    configure_otel() call's own console output -- pass False for a CLI
    script whose own printed output shouldn't be interleaved with a raw
    JSON decision/telemetry stream; see build_middleware()'s own
    docstring for the full story. The file sink (local_log_dir) and
    telemetry actually shipped to a control plane (once OTel is
    configured) are UNAFFECTED either way -- this only controls what
    prints locally.
    """

    def __init__(
        self,
        *args: Any,
        policy_dir: str | Path | None = None,
        entities_path: str | Path | None = None,
        agent_id: str | None = None,
        tenant: str = "default",
        control_plane_url: str | None = None,
        agent_secret: str | None = None,
        pep_key_path: str | Path | None = None,
        persist_policy_dir: str | Path | None = None,
        local_log_dir: str | Path | None = None,
        persist_pep_key: bool = True,
        otel_log_mode: Literal["streaming", "buffered"] = "buffered",
        console: bool = True,
        alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        chat_mw, func_mw = build_middleware(
            policy_dir,
            entities_path,
            agent_id,
            tenant,
            control_plane_url=control_plane_url,
            agent_secret=agent_secret,
            pep_key_path=pep_key_path,
            persist_policy_dir=persist_policy_dir,
            local_log_dir=local_log_dir,
            persist_pep_key=persist_pep_key,
            otel_log_mode=otel_log_mode,
            console=console,
            alter_transforms=alter_transforms,
        )
        extra_middleware = kwargs.get("middleware") or []
        kwargs["middleware"] = [chat_mw, func_mw, *extra_middleware]
        super().__init__(*args, **kwargs)
