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

import atexit
import contextlib
import contextvars
import json
import logging
import logging.handlers
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
from opentelemetry._logs import Logger as _OtelLogger
from opentelemetry._logs import SeverityNumber
from opentelemetry.context import Context as OtelContext
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import ConsoleLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode

import parapetai_agent.policy as parapetai_agent_policy
from parapetai_agent import pep_identity
from parapetai_agent._exceptions import GovernanceDenied
from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.control_plane import bootstrap_engine, sdk_version
from parapetai_agent.groundedness import GroundednessConfig
from parapetai_agent.identity import ANONYMOUS, Caller
from parapetai_agent.otel import openinference as oi
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook, content_free
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.response_judge import JudgeConfig
from parapetai_agent.token_identity import (
    ExtractedIdentity,
    JwtIdentityExtractor,
    TokenIdentityExtractor,
)

log = structlog.get_logger(__name__)

# trace.get_tracer() returns a lazy proxy -- verified directly that calling
# it here, at import time, before configure_otel() (or anything) has set a
# real TracerProvider, still correctly picks up the real provider once
# configure_otel() calls trace.set_tracer_provider(), rather than being
# permanently bound to the no-op default. Until configure_otel() runs,
# spans are harmless no-ops (trace_id=0) -- importing this module stays
# side-effect free either way.
_tracer = trace.get_tracer(__name__)
# None until configure_otel() runs; _emit_otel_decision() no-ops until then.
_otel_logger: _OtelLogger | None = None
# Held so flush_otel()/the atexit hook can reach them later -- OTel's own
# trace.get_tracer_provider() would work for the tracer half, but there's
# no equivalent global getter for the logger half, so both are stored
# explicitly here for symmetry rather than mixing global-lookup and
# module-state approaches.
_otel_tracer_provider: TracerProvider | None = None
_otel_logger_provider: LoggerProvider | None = None

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

# GovernanceDenied is defined at base level (parapetai_agent._exceptions) and
# imported above, so a denial is ONE catchable type across both this MAF adapter
# and the framework-neutral govern() facade, and is importable with no
# agent_framework dependency. `from parapetai_agent.maf import GovernanceDenied`
# still works via that import.


@dataclass(slots=True, frozen=True)
class _Identity:
    """What set_current_identity()/current_identity() puts in
    _current_identity -- see that ContextVar's docstring for why this
    exists at all (identity has no ambient source in MAF's own context
    objects; verified directly against AgentSession, which carries only
    session_id/service_session_id, nothing identity-shaped)."""

    claims: dict[str, str] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)


_current_identity: contextvars.ContextVar[_Identity | None] = contextvars.ContextVar(
    "parapetai_agent_maf_current_identity", default=None
)


def set_current_identity(
    *, claims: Mapping[str, Any] | None = None, roles: Sequence[Any] | None = None
) -> contextvars.Token[_Identity | None]:
    """Sets the end user's identity ambiently for every governed
    model_call/tool_call decision made from here on, in this asyncio task
    (and any child task created from within it -- ordinary contextvars
    copy-on-task-creation semantics), without repeating
    function_invocation_kwargs on every single agent.run() call.

    Why this exists: nothing in MAF's own request objects carries an
    ambient "current end user" -- verified directly, not assumed
    (AgentSession, the one plausible place, has only session_id/
    service_session_id; there is no HTTP request or session token this
    library can inspect on its own). Identity has to enter the system from
    the embedding application at least once. What this removes is having
    to re-enter it on every single agent.run() call: call this once (e.g.
    in a web framework's request middleware, right after validating a
    token) and every governed decision made while it's set picks it up
    automatically.

    Returns a contextvars.Token -- pass it to reset_current_identity() to
    restore whatever was set before (typically None), the same pattern
    contextvars.ContextVar.set()/.reset() already uses. Prefer the
    current_identity() context manager below for the common case of "set
    for the duration of one block"; use set_current_identity()/
    reset_current_identity() directly only when the set and the reset
    happen in genuinely different places (e.g. framework request/response
    hooks that don't share a single `with` block).

    An explicit identity_claims/identity_roles passed via
    agent.run(function_invocation_kwargs={...}) still takes precedence
    over whatever is set here -- see _identity_claims/_identity_roles.
    """
    identity = _Identity(
        claims={str(k): str(v) for k, v in (claims or {}).items()},
        roles=[str(r) for r in (roles or [])],
    )
    return _current_identity.set(identity)


def reset_current_identity(token: contextvars.Token[_Identity | None]) -> None:
    """Pairs with set_current_identity() -- see its docstring."""
    _current_identity.reset(token)


@contextlib.contextmanager
def current_identity(
    *, claims: Mapping[str, Any] | None = None, roles: Sequence[Any] | None = None
) -> Iterator[None]:
    """`with current_identity(claims=..., roles=...):` -- sets the end
    user's identity ambiently for every governed decision made anywhere
    inside this block (including everything it awaits), then restores the
    previous value on exit, even if the block raises. See
    set_current_identity()'s docstring for why this exists and how
    precedence against explicit function_invocation_kwargs works."""
    token = set_current_identity(claims=claims, roles=roles)
    try:
        yield
    finally:
        reset_current_identity(token)


# ── Agent identity: the OTHER half, distinct from the end-user identity
# above -- see the module docstring's "Two identities, not one" section.
# Optional by design (a real Service Principal identity, when available
# from a token, is more concrete than the developer-chosen agent_id string
# GovernedAgent otherwise requires -- see build_middleware()'s agent_id
# becoming optional, below), mirroring how end-user identity is optional.


@dataclass(slots=True, frozen=True)
class _AgentIdentity:
    """What set_current_agent_identity()/agent_identity() puts in
    _current_agent_identity -- claims here typically come from
    parapetai_agent.token_identity.agent_identity_from_claims() (an RFC 8693
    `act` claim, or an azp/appid fallback), but this module doesn't
    require that specific source; anything can call
    set_current_agent_identity() directly."""

    claims: dict[str, str] = field(default_factory=dict)


_current_agent_identity: contextvars.ContextVar[_AgentIdentity | None] = contextvars.ContextVar(
    "parapetai_agent_maf_current_agent_identity", default=None
)


def set_current_agent_identity(
    *, claims: Mapping[str, Any] | None = None
) -> contextvars.Token[_AgentIdentity | None]:
    """Sets the AGENT's own identity ambiently (e.g. a Service Principal's
    client_id, decoded from a token's RFC 8693 act claim or azp/appid --
    see parapetai_agent.token_identity), overriding the static agent_id a
    Caller/GovernedAgent was constructed with for as long as it's set --
    see _effective_principal(). Same set()/reset(token) shape as
    set_current_identity(); see that function's docstring for the general
    rationale (ambient, contextvars-backed, correctly isolated per
    asyncio task)."""
    return _current_agent_identity.set(
        _AgentIdentity(claims={str(k): str(v) for k, v in (claims or {}).items()})
    )


def reset_current_agent_identity(token: contextvars.Token[_AgentIdentity | None]) -> None:
    """Pairs with set_current_agent_identity() -- see its docstring."""
    _current_agent_identity.reset(token)


# Per-request bucket of tool-call denial REASONS, so an embedding app can tell
# "the model answered" apart from "a tool the model tried to call was denied".
# A denied tool_call never raises (see this module's docstring: MAF's own
# function-invocation loop swallows a raise from FunctionMiddleware, so
# ParapetFunctionMiddleware substitutes a GOVERNANCE_DENIED result STRING
# instead of raising). Without this, that substituted string flows back as
# ordinary tool output and the model paraphrases it into a normal-looking
# answer, hiding the denial behind a green "here you go" bubble -- a real,
# previously-shipped bug. track_tool_denials() opens a bucket for the duration
# of one agent.run(); the middleware appends each denial reason to whatever
# bucket is active. None (the default) means "nobody is tracking", so
# recording is a no-op and the middleware pays nothing outside a tracked call.
# Same contextvars discipline as _current_chat/_current_identity above: set on
# entry, reset on exit, correctly isolated per asyncio task.
_tool_denials: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "parapetai_tool_denials", default=None
)


@contextlib.contextmanager
def track_tool_denials() -> Iterator[list[str]]:
    """Collect the reason of every tool_call ParapetFunctionMiddleware denies
    during the wrapped block (typically one agent.run()). Yields the list the
    middleware appends to; read it AFTER the block to see whether any tool was
    blocked::

        with track_tool_denials() as denials:
            await agent.run(...)
        if denials:
            ...  # a tool call was governed away -- don't trust the model's text

    An empty list means no tool_call was denied; a turn can still be denied at
    the model_call or content-check stage, which raise GovernanceDenied instead
    -- those are separate signals. Nesting opens an independent inner bucket
    and the outer one resumes on exit."""
    bucket: list[str] = []
    token = _tool_denials.set(bucket)
    try:
        yield bucket
    finally:
        _tool_denials.reset(token)


def _record_tool_denial(reason: str) -> None:
    """Append a tool_call denial reason to the active track_tool_denials()
    bucket, if one is open. A no-op when nothing is tracking (the common
    case), so ParapetFunctionMiddleware can call it unconditionally at every
    deny without caring whether a caller is listening."""
    bucket = _tool_denials.get()
    if bucket is not None:
        bucket.append(reason)


@contextlib.contextmanager
def agent_identity(*, claims: Mapping[str, Any] | None = None) -> Iterator[None]:
    """`with agent_identity(claims={"client_id": "..."}):` -- the
    context-manager form of set_current_agent_identity()/
    reset_current_agent_identity(), for the common case of "set for the
    duration of one block". Usually reached via
    identity_from_bearer_token() rather than called directly."""
    token = set_current_agent_identity(claims=claims)
    try:
        yield
    finally:
        reset_current_agent_identity(token)


def _effective_principal(caller: Caller) -> str:
    """Cedar's principal for this decision. Ambient agent identity (set
    via agent_identity()/identity_from_bearer_token(), typically from a
    real token's act/azp/appid claim -- a genuine Service Principal
    identity) takes precedence over caller.principal (the static
    agent_id string a Caller/GovernedAgent was constructed with) when
    present; otherwise falls back to caller.principal exactly as before
    this existed. This is what makes GovernedAgent(agent_id=...)'s
    developer-chosen string optional in practice once a real identity is
    available from a token, without requiring it be chosen upfront."""
    agent = _current_agent_identity.get()
    if agent and agent.claims:
        identifier = (
            agent.claims.get("client_id")
            or agent.claims.get("oid")
            or agent.claims.get("sub")
            or "unknown"
        )
        return f'Agent::"{identifier}"'
    return caller.principal


def identity_from_bearer_token(
    token: str, *, extractor: TokenIdentityExtractor | None = None
) -> _CombinedIdentityContext:
    """The MCP-facing entry point: decodes ONE bearer token (the same one
    used for an outbound MCP connection's `Authorization: Bearer <token>`
    header -- see parapetai_agent.token_identity's module docstring for
    exactly what MCP's own spec requires and doesn't) into BOTH end-user
    and agent identity, and sets both ambiently for the duration of a
    `with` block:

        headers = {"Authorization": f"Bearer {token}"}
        with identity_from_bearer_token(token):
            async with MCPStreamableHTTPTool(url=..., headers=headers) as mcp_tool:
                ...

    extractor defaults to token_identity.JwtIdentityExtractor(); pass a
    different TokenIdentityExtractor (see that Protocol's docstring) to
    support a non-JWT token format. Either half of the extracted identity
    may come back empty -- that's not an error, see ExtractedIdentity's
    docstring -- and this still sets both context managers regardless, so
    "asserted but empty" (a real user with zero roles, or a token with no
    delegation) is preserved correctly rather than collapsed into
    "nothing was asserted at all" (see Snapshot.to_context()'s own
    docstring for why that distinction matters to Cedar's `has` checks).
    """
    identity = (extractor or JwtIdentityExtractor()).extract(token)
    return _CombinedIdentityContext(identity)


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


class _CombinedIdentityContext:
    """Backs identity_from_bearer_token()'s `with` block -- a plain class,
    not @contextlib.contextmanager, because it needs to enter/exit TWO
    independent context managers (end-user + agent identity) together and
    still restore each correctly even if only one was ever set."""

    def __init__(self, identity: ExtractedIdentity) -> None:
        self._identity = identity
        self._user_token: contextvars.Token[_Identity | None] | None = None
        self._agent_token: contextvars.Token[_AgentIdentity | None] | None = None

    def __enter__(self) -> None:
        self._user_token = set_current_identity(
            claims=self._identity.end_user_claims, roles=self._identity.end_user_roles
        )
        if self._identity.agent_claims:
            self._agent_token = set_current_agent_identity(claims=self._identity.agent_claims)

    def __exit__(self, *exc_info: object) -> None:
        if self._user_token is not None:
            reset_current_identity(self._user_token)
        if self._agent_token is not None:
            reset_current_agent_identity(self._agent_token)


def _identity_claims(kwargs: Mapping[str, Any] | None) -> dict[str, str]:
    """Explicit function_invocation_kwargs wins if present; otherwise
    falls back to whatever set_current_identity()/current_identity() set
    ambiently for the current asyncio task. Falling back, not merging: an
    explicit per-call identity_claims fully replaces the ambient one
    (matches how you'd expect an explicit override to behave), it doesn't
    get combined with it field-by-field."""
    if kwargs:
        claims = kwargs.get("identity_claims")
        if isinstance(claims, dict):
            return {str(k): str(v) for k, v in claims.items()}
    ambient = _current_identity.get()
    return dict(ambient.claims) if ambient else {}


def _identity_roles(kwargs: Mapping[str, Any] | None) -> list[str]:
    """A role claim (e.g. Entra ID app roles) is a SET, not a scalar --
    kept separate from _identity_claims for the same reason
    Snapshot.identity_roles is its own field, not folded into
    identity_claims. See that field's docstring. Same
    explicit-kwargs-first, ambient-fallback precedence as
    _identity_claims."""
    if kwargs:
        roles = kwargs.get("identity_roles")
        if isinstance(roles, (list, tuple)):
            return [str(r) for r in roles]
    ambient = _current_identity.get()
    return list(ambient.roles) if ambient else []


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


def _audit(
    decision: Decision,
    principal: str,
    snapshot: Snapshot,
    resource: str,
    context: Mapping[str, Any],
) -> None:
    """Same event name and shape as parapetai_gateway.server.app._audit's "decision" event
    for the HTTP path -- existing tooling (CLAUDE.md's dev loop:
    `jq 'select(.event=="decision")'`) works unchanged against this path too.
    messages_preview/response_preview/tool_result_preview are stripped for
    the same content-free-by-construction reason as the HTTP path -- see
    CLAUDE.md invariant 10; parapetai_agent.policy.hooks.content_free is the
    same strip GovernanceHook's own default audit callback uses, shared so
    the post-call preview fields added for ALTER/OBSERVE can't be missed
    here the way messages_preview almost was. determining_policies (the
    Cedar rule ids that decided the request) rides through
    Decision.to_audit_record() unchanged.

    `principal` is taken as the caller already resolved it (via
    _effective_principal()), not re-derived here -- so what's audited
    always matches what Cedar actually evaluated, including when ambient
    agent identity (set_current_agent_identity()/identity_from_bearer_token())
    overrode a Caller's static agent_id for this decision.

    Feeds BOTH sinks unconditionally -- the rotating-file/stdout structlog
    JSON (configure_rotating_audit_log) and the OTel LogRecord
    (configure_otel) -- each a no-op until its own configure_*() has been
    called, so callers can use either, both, or neither."""
    stripped_context = content_free(context)
    record = decision.to_audit_record(
        principal=principal,
        action=snapshot.action,
        resource=resource,
        context=stripped_context,
    )
    log.info("decision", **record)
    _emit_otel_decision(record, stripped_context)


def _emit_otel_decision(record: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    """Real OTel LogRecord for the same decision _audit() just wrote as
    structlog JSON. No-op until configure_otel() has been called, so
    importing this module stays side-effect free.

    Body/Attributes split, not one flat dict: Body ("decision") is the
    event/message OTel's model expects (-> Azure Monitor AppTraces.Message);
    `record` itself (principal, action, decision, reason,
    determining_policies, context, ...) becomes Attributes
    (-> AppTraces.Properties/customDimensions).

    identity_claims.oid is additionally promoted to the enduser.id
    semantic-convention attribute, and identity_roles to user.roles (the
    current, non-deprecated convention -- enduser.role is deprecated in
    its favour, confirmed against the OTel semantic conventions registry,
    not assumed). Azure Monitor gives enduser.id special handling
    specifically (-> the queryable user_AuthenticatedId column, confirmed
    against Microsoft's own docs) rather than leaving it buried in
    customDimensions like an arbitrary key.

    This is deliberately the END USER's identity (e.g. Bob, from a real
    Entra sign-in), not the same thing as record["principal"] (the calling
    AGENT's own identity, e.g. Agent::"example-entra") -- see the module
    docstring's "Two identities, not one" section. TraceId/SpanId are NOT
    set explicitly here: Logger.emit() with no explicit context pulls them
    from the currently active span automatically (verified directly against
    opentelemetry-sdk's LogRecord construction, not assumed) -- which is
    real and correct here specifically because _audit() is always called
    from inside ParapetChatMiddleware/ParapetFunctionMiddleware's own
    `with _tracer.start_as_current_span(...)` block.
    """
    if _otel_logger is None:
        return
    attributes: dict[str, Any] = dict(record)
    identity_claims = context.get("identity_claims")
    identity_roles = context.get("identity_roles")
    if isinstance(identity_claims, dict) and identity_claims.get("oid"):
        attributes["enduser.id"] = identity_claims["oid"]
    if isinstance(identity_roles, list) and identity_roles:
        attributes["user.roles"] = list(identity_roles)
    _otel_logger.emit(
        body="decision",
        severity_number=SeverityNumber.INFO,
        severity_text="INFO",
        attributes=attributes,
    )


_audit_log_configured_dirs: set[str] = set()


def configure_rotating_audit_log(
    log_dir: str | Path,
    *,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> Path:
    """Routes every structlog event from this module (and from PolicyEngine --
    "policy_loaded", "policy_reload_rejected", etc.) to a size-bounded,
    rotating JSON-lines file under log_dir, in addition to stdout
    (console=True, the default -- pass console=False to write ONLY the
    file, e.g. for a CLI example whose own printed output shouldn't be
    interleaved with a JSON decision stream).

    The HTTP path (parapetai_gateway.server.app) has never needed this: operators
    redirect its stdout themselves (CLAUDE.md's dev loop: `make dev >
    /tmp/parapetai.log`). The in-process path has no equivalent convention --
    it's a library import inside someone else's application, not a process
    this repo owns the entrypoint of -- so callers that want a persistent,
    bounded decision trail call this once, before build_middleware() (or
    pass local_log_dir= to GovernedAgent/build_middleware, which calls
    this internally -- same effect, one less line at the call site).

    Not called automatically on import: importing this module must stay
    side-effect free (CLAUDE.md: "interop is never a runtime dependency
    of the core gateway"), and this reconfigures process-wide logging,
    which only the embedding application should decide to do -- but IS
    idempotent per log_dir (a real requirement once GovernedAgent could
    call this itself: constructing several GovernedAgents with the same
    local_log_dir in one process, e.g. one per chat session in a web app,
    must not attach a duplicate pair of handlers -- and therefore log
    every line twice -- on each one). A second call with a DIFFERENT
    log_dir still attaches its own additional handler pair; only an
    identical, already-configured log_dir is skipped -- console is only
    read on the FIRST call for a given log_dir, same as max_bytes/
    backup_count.
    """
    resolved = str(Path(log_dir).resolve())
    if resolved in _audit_log_configured_dirs:
        return Path(log_dir) / "parapetai-decisions.jsonl"
    _audit_log_configured_dirs.add(resolved)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "parapetai-decisions.jsonl"

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers: list[logging.Handler] = [file_handler]
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(stream_handler)

    # Scoped to the "parapetai_agent" and "parapetai_agent" loggers specifically, not
    # root: structlog.get_logger(__name__) in this module produces a logger
    # named "parapetai_agent.maf"; PolicyEngine/control_plane/pep_identity (in
    # the separate parapetai_agent package this module depends on) produce
    # "parapetai_agent.*" ones -- TWO different top-level namespaces post-split
    # (they used to share one, "parapetai.*", back when this was all one
    # package -- an attach-to-"parapetai" version of this existed then and
    # would silently catch nothing today if it had just been renamed
    # in place rather than re-derived). Both get their own logger + handler
    # pair here rather than a single shared one, since there's no longer a
    # common prefix to attach to. Unrelated libraries' own loggers (httpx's
    # request logging, the MCP SDK's session logging, ...) are named
    # independently and never reach either handler -- attaching to root
    # instead was tried, in this module's original single-package form, and
    # pulled all of that noise into what's supposed to be a narrow decision
    # trail, found by inspecting the actual log file, not assumed clean.
    # propagate=False on both so records aren't also duplicated to whatever
    # the embedding application's own root handler does.
    for logger_name in ("parapetai_agent", "parapetai_agent"):
        pkg_logger = logging.getLogger(logger_name)
        pkg_logger.setLevel(logging.INFO)
        for handler in handlers:
            pkg_logger.addHandler(handler)
        pkg_logger.propagate = False

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    return log_path


_DEFAULT_BATCH_SCHEDULE_DELAY_S = 120.0  # 2 minutes -- see log_mode's own docstring


def configure_otel(
    *,
    service_name: str = "parapetai-interop-maf",
    azure_monitor_connection_string: str | None = None,
    otlp_endpoint: str | None = None,
    otlp_headers: dict[str, str] | None = None,
    console: bool = True,
    log_mode: Literal["streaming", "buffered"] = "buffered",
    batch_max_size: int = 512,
    batch_schedule_delay_s: float = _DEFAULT_BATCH_SCHEDULE_DELAY_S,
) -> None:
    """Wires this module's model_call/tool_call decisions into real
    OpenTelemetry trace correlation and LogRecord emission -- see the
    module docstring's "OpenTelemetry / Azure Monitor compatibility"
    section for exactly what gap this closes and what was verified (not
    assumed) about the OTel Log Data Model and Azure Monitor's own field
    mapping.

    Sets a PROCESS-WIDE TracerProvider and LoggerProvider (OTel's own
    global-registration model, not one per Caller/PolicyEngine) --
    Resource.service.name therefore describes the process as a whole, not
    any single Cedar principal, which varies per build_middleware() call
    and is carried per-record instead (record["principal"], already in
    every decision).

    console=True (default) always exports to stdout in OTel's own
    ConsoleSpanExporter/ConsoleLogExporter format via a SimpleSpanProcessor/
    SimpleLogRecordProcessor -- immediate, unbuffered, independent of
    log_mode below (console output is for live debugging, not a real
    transport with a cost-per-call worth batching). azure_monitor_connection_string
    is opt-in: pass os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    (Azure's own standard env var name for this) to also ship the same
    spans/logs to a real Application Insights resource via
    azure-monitor-opentelemetry-exporter -- imported lazily, only when a
    connection string is actually supplied, so it's never a hard
    dependency of this module (CLAUDE.md: "interop is never a runtime
    dependency of the core gateway" -- the same opt-in-extra discipline
    applies one level down, to Azure export specifically, within this
    already-optional module).

    otlp_endpoint is opt-in like azure_monitor_connection_string, and
    independent of it -- both, either, or neither may be set. Pass the
    control plane's base URL (e.g. from PARAPETAI_OTLP_ENDPOINT /
    settings.otlp_endpoint) to also ship spans/logs to
    parapetai_control/otlp.py's real OTLP/HTTP (protobuf) receiver via genuine
    OTLPSpanExporter/OTLPLogExporter -- `{otlp_endpoint}/v1/traces` and
    `{otlp_endpoint}/v1/logs`, the OTLP spec's own standard per-signal
    paths, so this is not a control-plane-specific wire format on either
    side. otlp_headers should carry the agent's bearer secret (e.g.
    {"Authorization": f"Bearer {agent_secret}"}) since the OTLP wire format
    itself carries no agent identity -- parapetai_control.auth resolves the agent from
    that header the same way parapetai_control/api.py's bundle endpoint does.

    log_mode governs BOTH external exporters above (azure_monitor and
    otlp) uniformly -- both are "ship telemetry off this process" sinks
    with the identical streaming-vs-buffered tradeoff, not two separate
    concerns:
      - "buffered" (default): SimpleSpanProcessor/SimpleLogRecordProcessor
        swapped for BatchSpanProcessor/BatchLogRecordProcessor --
        accumulates up to batch_max_size records (default 512, OTel's
        own default) OR batch_schedule_delay_s seconds (default 120 --
        TWO MINUTES, deliberately overriding OTel's own 5-second default,
        which is aggressive enough to matter for a chatty agent process)
        since the LAST export, whichever comes first, then sends one
        batch. Lower request volume to the control plane at the cost of
        up to batch_schedule_delay_s of latency before a decision shows
        up there -- and see flush_otel()/the atexit hook this function
        registers for why that delay does NOT mean losing data on a
        normal process exit.
      - "streaming": every span/LogRecord exports the moment it's
        emitted, same as the console path. Lower latency, higher request
        volume -- appropriate for low-traffic agents or when you want a
        Cedar decision visible in the control plane immediately, not
        worth it for a busy production agent.

    Independent of configure_rotating_audit_log(): call either, both, or
    neither. _audit() feeds both sinks unconditionally; each is a no-op
    until its own configure_*() has run.

    Registers an atexit hook that flushes+shuts down both providers on
    NORMAL interpreter exit (script end, uncaught exception, sys.exit())
    -- covers every CLI-shaped example in this repo with zero extra code
    at the call site. Does NOT fire on SIGTERM (Python's default SIGTERM
    disposition terminates the process without running atexit handlers;
    installing a competing signal.signal(SIGTERM, ...) handler here would
    risk stealing the signal from whatever the embedding application's
    OWN graceful-shutdown path already does with it, e.g. uvicorn's own
    SIGTERM handling funneling into a FastAPI lifespan's `finally:`
    block) -- a long-running server should call flush_otel() explicitly
    from ITS OWN shutdown sequence instead; see
    examples/maf_webapp/web_app.py's lifespan() for exactly that.
    """
    resource = Resource.create({"service.name": service_name, "service.namespace": "parapetai"})

    tracer_provider = TracerProvider(resource=resource)
    logger_provider = LoggerProvider(resource=resource)

    if console:
        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogExporter()))

    def _add_external_processors(span_exporter: Any, log_exporter: Any) -> None:
        if log_mode == "streaming":
            tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
            logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        else:
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            schedule_delay_millis = batch_schedule_delay_s * 1000
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    span_exporter,
                    max_export_batch_size=batch_max_size,
                    schedule_delay_millis=schedule_delay_millis,
                )
            )
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    log_exporter,
                    max_export_batch_size=batch_max_size,
                    schedule_delay_millis=schedule_delay_millis,
                )
            )

    if azure_monitor_connection_string:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorTraceExporter,
        )

        _add_external_processors(
            AzureMonitorTraceExporter.from_connection_string(azure_monitor_connection_string),
            AzureMonitorLogExporter.from_connection_string(azure_monitor_connection_string),
        )

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        base = otlp_endpoint.rstrip("/")
        _add_external_processors(
            OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=otlp_headers),
            OTLPLogExporter(endpoint=f"{base}/v1/logs", headers=otlp_headers),
        )

    trace.set_tracer_provider(tracer_provider)
    global _otel_logger, _otel_tracer_provider, _otel_logger_provider
    _otel_logger = logger_provider.get_logger(__name__)
    _otel_tracer_provider = tracer_provider
    _otel_logger_provider = logger_provider
    atexit.register(flush_otel)


def flush_otel() -> None:
    """Flushes any buffered (BatchSpanProcessor/BatchLogRecordProcessor)
    spans/LogRecords and shuts down both providers -- safe to call
    multiple times (OTel's own shutdown() is itself idempotent) and safe
    to call before configure_otel() has ever run (no-op, nothing to
    flush). Registered automatically as an atexit hook by configure_otel()
    for the normal-process-exit case; call this explicitly from a
    long-running server's OWN shutdown sequence (e.g. a FastAPI
    lifespan's `finally:` block) for the SIGTERM/graceful-shutdown case
    atexit alone doesn't cover -- see configure_otel()'s own docstring
    for why this isn't wired to a signal handler here instead."""
    if _otel_tracer_provider is not None:
        _otel_tracer_provider.shutdown()
    if _otel_logger_provider is not None:
        _otel_logger_provider.shutdown()


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


def _unresolved_alter_decision(policy_generation: int, name: str) -> Decision:
    """A synthetic deny for the one case Cedar itself never produces: an
    ALLOW whose determining policy named an @alter_with transform that
    isn't registered on this process. Fails closed per invariant 1 -- an
    unresolvable alter must never fall through to passing the original,
    unaltered content through unchanged."""
    reason = f"unregistered alter transform: {name!r}"
    return Decision(False, "deny", reason, policy_generation, 0.0, errors=(reason,))


def _content_check_failure_decision(policy_generation: int, errors: tuple[str, ...]) -> Decision:
    """A synthetic deny for a configured tier-2 content check that could
    not run (unknown scanner_id, or the scanner raised -- see
    parapetai_agent.content_checks's own module docstring for the full
    reasoning). Fails closed per invariant 1: called BEFORE
    GovernanceHook.evaluate(), so Cedar never runs and can never treat
    the resulting missing context.content_checks key as "nothing to
    check" -- the same silent-bypass shape an unresolved ALTER transform
    would have if _unresolved_alter_decision above didn't exist."""
    reason = f"content check scanner failure: {'; '.join(errors)}"
    return Decision(False, "deny", reason, policy_generation, 0.0, errors=errors)


def _log_content_enabled() -> bool:
    """PARAPETAI_OTEL_LOG_CONTENT, default false -- opt-in gate for every
    OpenInference content_bearing attribute (full prompt/response/tool-arg
    text riding on a span), mirroring gateway/server/app.py's
    PARAPETAI_LOG_PROMPTS (ADR 0005) one layer over: that gates a structlog
    event, this gates span attributes. See docs/adr/0007. Read fresh on
    every call rather than cached at import time, so a test can
    monkeypatch it per-case the same way PARAPETAI_LOG_PROMPTS's own tests do."""
    return os.environ.get("PARAPETAI_OTEL_LOG_CONTENT", "false").lower() == "true"


def _oi_attribute_value(value: Any) -> str | bool | int | float | tuple[Any, ...]:
    """OTel span attribute values must be a primitive or a homogeneous
    sequence of primitives (opentelemetry.util.types.AttributeValue) --
    the same constraint parapetai_agent.policy.hooks._otel_attribute_value
    enforces for governance attributes. Duplicated here rather than
    importing a "_"-prefixed name across the parapetai-agent/parapetai-agent package
    boundary; it's a tiny, pure coercion with no state to drift."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (str, bool, int, float)) for v in value
    ):
        return tuple(value)
    return json.dumps(value, default=str)


def _set_oi_attributes(span: Any, attributes: dict[str, Any]) -> None:
    """Sets OpenInference attributes on `span`, skipping None values --
    same is_recording() guard parapetai_agent.policy.hooks._set_span_attributes
    uses, so this is a genuine no-op with no tracer configured (e.g. no
    control plane wired up). Callers are responsible for only ever passing
    a content_bearing key (see parapetai_agent.otel.openinference.BY_KEY) when
    _log_content_enabled() is true -- this function does not itself check
    per-key sensitivity, matching how content_free() strips at the call
    site rather than the sink."""
    if not span.is_recording():
        return
    span.set_attributes({k: _oi_attribute_value(v) for k, v in attributes.items() if v is not None})


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
    "lookup", "get", "search", "find", "list", "fetch", "retrieve",
    "read", "query", "describe", "show", "view", "load", "select",
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
            extra_context = content_result.context if content_result else None
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
                    context, principal, correlation, identity_claims, identity_roles
                )
                return

            chat_response = context.result
            if not isinstance(chat_response, ChatResponse):
                return  # an earlier middleware already overrode/denied -- nothing of ours to check
            _set_oi_attributes(span, _token_count_attributes(chat_response.usage_details))
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
            post_context: dict[str, Any] = {}
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
        non-streaming path, not an oversight."""

        async def _audit_finalized(finalized: ChatResponse) -> ChatResponse:
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
            pre = self.hook.evaluate(snapshot=snapshot, stage="pre", principal=principal)
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
            post = self.hook.evaluate(snapshot=response_snapshot, stage="post", principal=principal)
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


# _installed_version() used to live here and asked importlib.metadata for
# "parapetai-gateway" -- a copy-paste of the gateway's own identical helper,
# where that name is right. In an embedded SDK the gateway package is normally
# absent, so every SDK PEP heartbeat reported "0.0.0-dev". Replaced by
# control_plane.sdk_version(), which reports this package; see its docstring.


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


def _bundled_default_policy_dir() -> Path:
    # Lives in parapetai-agent (parapetai-agent/src/parapetai_agent/policy/
    # default_policies/), not parapetai-agent -- co-located with PolicyEngine,
    # the class that loads it, and available to any consumer of
    # parapetai-agent's policy engine, not just this in-process embedding
    # path. Resolved relative to parapetai_agent.policy's own package file,
    # not this module's -- correct regardless of which package's wheel
    # each ends up installed under.
    return Path(parapetai_agent_policy.__file__).resolve().parent / "default_policies"


def _resolve_policy_source(
    policy_dir: str | Path | None, entities_path: str | Path | None
) -> tuple[Path, Path | None]:
    """Both policy_dir/entities_path are optional now -- this is what
    makes that possible without weakening default-deny. Omitting
    policy_dir uses the policy set bundled INSIDE this package
    (default_policies/, a real, read-only file installed alongside this
    module -- confirmed live it's included in a built wheel, not just
    visible in an editable/source checkout), so a brand new
    GovernedAgent(...) call enforces something real (base permits only,
    same shape as this repo's own policies/00-base.cedar) from the
    moment it's constructed, never a required setup step, and never
    requires a writable filesystem -- works in a read-only container
    (k8s, serverless) with no mounted volume. This is a STARTING POINT:
    once a real control-plane-provisioned agent is configured, its real
    bundle takes over (see build_middleware()'s control-plane section).

    Omitting entities_path when policy_dir IS given looks for an
    entities.json alongside it (this repo's own convention) but doesn't
    require one to exist -- PolicyEngine already treats a missing/None
    entities_path as zero entities, never an error, so a caller with no
    entities to define isn't forced to create an empty file just to
    satisfy this function's signature."""
    if policy_dir is None:
        default_dir = _bundled_default_policy_dir()
        resolved_entities = (
            Path(entities_path) if entities_path is not None else default_dir / "entities.json"
        )
        return default_dir, resolved_entities
    resolved_dir = Path(policy_dir)
    if entities_path is not None:
        return resolved_dir, Path(entities_path)
    candidate = resolved_dir / "entities.json"
    return resolved_dir, candidate if candidate.exists() else None


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

    if control_plane_configured and _otel_tracer_provider is None:
        # See this function's own docstring's "OpenTelemetry is wired up
        # automatically too" paragraph. _otel_tracer_provider is None
        # check makes this idempotent AND yields to an embedder's own,
        # earlier configure_otel() call (e.g. for Azure Monitor export)
        # -- first call wins, matching OTel's own set-once global
        # registration.
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

        # None (not default_key_path()) when persist_pep_key=False -- a
        # real path here would be misleading (nothing is ever written to
        # it) and run_bundle_poller's own rotate_key handling already
        # treats key_path=None as "nowhere safe to persist a rotation,
        # log and skip" (see its own docstring), exactly the right
        # behavior for an identity that's regenerated fresh every
        # restart anyway -- there's nothing a rotation could persist TO
        # that would survive the next one.
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
            # Identity registration, first fetch, disk-vs-memory choice,
            # heartbeat and poller thread all live in ONE place now
            # (parapetai_agent.control_plane.bootstrap_engine), shared with
            # Governor.from_control_plane(). Two copies meant two sets of
            # outage semantics, so "the agent acts as configured" could mean
            # something different depending on which integration a customer
            # picked. Behaviour here is unchanged -- see that function's
            # docstring for the persist_policy_dir / in-memory split this
            # used to spell out inline.
            boot = bootstrap_engine(
                resolved_control_plane_url,
                resolved_agent_secret,
                policy_dir=resolved_policy_dir,
                entities_path=resolved_entities_path,
                persist_policy_dir=persist_policy_dir,
                pep_key_path=resolved_pep_key_path,
                mode="enforce",  # build_middleware always enforces; no monitor-only mode here
                version=sdk_version(),
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
