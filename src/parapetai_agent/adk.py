"""In-process governance for Google ADK (Agent Development Kit,
`google-adk` on PyPI).

Part of the `parapetai-agent` package -- open source, the thing an agent
framework integrator imports directly into their own process. Same product
as `parapetai_agent.maf` (see that module's own docstring for the
gateway-vs-in-process distinction), a second framework behind it. Runs
INSIDE the agent process via ADK's own Plugin hook system
(`google.adk.plugins.base_plugin.BasePlugin`, registered on a `Runner`) --
there is no HTTP request, no path prefix, no wire bytes. Builds the same
Snapshot/Cedar-evaluation contract the HTTP gateway and `parapetai_agent.maf`
use (`parapetai_agent.providers.parsers.Snapshot`,
`parapetai_agent.policy.engine.PolicyEngine`), just populated from ADK's own
request/response objects.

Requires the `google-adk` package -- install via the `adk` extra
(`pip install parapetai-agent[adk]`). Never imported by
`parapetai_gateway.server.app`, and never imports `agent_framework` --
`parapetai_agent.maf`'s own optional dependency stays independent of this
module's, so `pip install parapetai-agent[adk]` alone never pulls in MAF,
and vice versa. Shares `parapetai_agent.scoped_data` (end-user/agent
identity) and `parapetai_agent.governance_runtime` (OTel wiring, the
"decision" audit event, a few Cedar-decision helpers) with `maf.py` --
those two modules are the framework-agnostic seam CLAUDE.md's
architecture calls for; this module and `maf.py` are the only things that
build a Snapshot from a framework's own objects, per
`parapetai_agent.policy.hooks`'s own stated contract.

## Where ADK's governable seam actually is

Unlike MAF, where `Agent(middleware=[...])` is the interception point, an
ADK `Agent` has no model-call middleware concept of its own. The seam is
`Runner`, via a `plugins: list[BasePlugin]` constructor kwarg -- one
registration, global across every agent/model/tool call that `Runner`
drives (confirmed against `google-adk` 2.7.1's own `Runner.__init__` and
`BasePlugin` source, not assumed from docs). That is why this module's
drop-in replacement is `GovernedRunner`, not a `GovernedAgent` -- forcing
an identical name across two frameworks whose own architecture puts the
governable seam on a DIFFERENT class would be misleading about what's
actually being wrapped, and would collide at `parapetai_agent`'s top-level
namespace if both the `maf` and `adk` extras are installed in the same
process.

`BasePlugin`'s deny mechanism is cleaner than MAF's needs to be, too:
returning a non-`None` `LlmResponse` from `before_model_callback` skips the
real model call and substitutes the returned response (ADK's own
documented "early exit"); returning a non-`None` `dict` from
`before_tool_callback` skips the real tool call the same way and becomes
the tool's result directly. Neither needs MAF's raised-exception-that-gets-
silently-swallowed workaround (`GovernanceDenied`'s docstring, and
`maf.py`'s "Enforcement asymmetry" section, describe exactly that gap on
the MAF side) -- confirmed against `BasePlugin`'s own docstrings, which
state this contract explicitly (not inferred).

## Streaming

Confirmed against `google-adk` 2.7.1's flow source (not assumed):
`before_model_callback` fires ONCE per turn -- a real, hard pre-call gate,
same strength as MAF's. `after_model_callback` fires ONCE PER STREAMED
CHUNK when `RunConfig.streaming_mode == StreamingMode.SSE` -- each a
separate `LlmResponse` with `partial=True`, until one final response with
`partial` false/unset carries the complete content (`LlmResponse`'s own
docstring: "a producer emits zero or more fragments and then one response
with partial false"). This module relays every partial chunk unmodified
(returns `None`) and evaluates the post-call Cedar decision only once, on
the final chunk, against the ACCUMULATED text -- deliberately not
per-chunk evaluation, both to stay consistent with `parapetai_agent.maf`'s
proven, tested pattern and per CLAUDE.md invariant 6 ("never buffer or
reorder chunks for parsing convenience" -- buffering here is for
EVALUATION, not to alter relay order, and no chunk is ever delayed or
reordered). Because `after_model_callback` runs BEFORE each chunk reaches
the caller (unlike MAF, where a finalized-stream hook only fires after
every chunk has already been delivered -- see `maf.py`'s own module
docstring), the FINAL chunk genuinely can be denied/altered before it goes
out; any EARLIER partial chunks were already relayed before enough text
existed to evaluate. A real, documented asymmetry, not a silent gap --
and NOT yet verified against a live streaming ADK agent (this module was
built by reading `google-adk` 2.7.1's source directly, not by driving a
live streaming call through it end to end). Verify that live before relying
on this in a production streaming deployment.

## Identity

Reuses `parapetai_agent.scoped_data` unchanged -- `governed_identity()`/
`current_identity()`/`identity_from_bearer_token()` work identically
whether the embedding app is using `parapetai_agent.maf.GovernedAgent` or
this module's `GovernedRunner`; an app switching frameworks changes zero
identity code. That ambient identity always wins when present.

ADK additionally offers its own identity-shaped field, `Session.user_id`
(a plain string, no claims/roles) -- but it is NOT used as a fallback by
default. `Runner.run_async()` requires `user_id` unconditionally
(confirmed against `runners.py`'s `_get_or_create_session()`) and never
authenticates it -- it is exactly as trusted as any other variable the
calling code assigned itself. Folding it into `identity_claims` by
default would mean an identity-gated Cedar policy (e.g.
`policies/30-identity.cedar`'s `context has identity_claims` guard,
distinguishing "no identity asserted" from "identity asserted but missing
a role") silently enforces a STRICTER default for ADK than for MAF on the
exact same policy bundle -- verified live: with the fallback defaulted
on, a caller that never logs in at all still got denied a role-gated tool
call MAF would have allowed. `build_plugin()`/`GovernedRunner`'s
`trust_session_user_id=True` (default `False`) opts back in, for a
deployment that already knows its own `user_id` values come from
somewhere trustworthy. See `_resolved_identity_claims()`'s own docstring
for the full reasoning.

## Known gaps (not built, not broken -- see CLAUDE.md's own "Known gaps")

- No groundedness/response-judge (QUAL-1) integration yet -- `maf.py` has
  both; this module only wires Cedar pre/post + tier-2 content checks.
- No `run_live()` (BIDI/audio) support -- only `run()`/`run_async()`'s
  `NONE`/`SSE` streaming modes are handled; a live/bidi turn is ungoverned
  by this plugin as of this writing.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog
from google.adk.agents.base_agent import BaseAgent as AdkBaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.run_config import StreamingMode  # type: ignore[attr-defined]
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService

# StreamingMode is actually defined in google.adk.agents._streaming_mode and
# re-exported (without an __all__) from run_config -- the documented,
# public import path (confirmed against google-adk's own usage), but
# google-adk's py.typed marker makes mypy --strict's no-implicit-reexport
# check flag it anyway since run_config.py itself never declares __all__.
# Importing from the private _streaming_mode module instead would be more
# "correct" by mypy's rule but more fragile in practice (an underscore-
# prefixed module is exactly the kind of thing a future google-adk release
# is free to rename).
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, Status, StatusCode

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
from parapetai_agent.identity import ANONYMOUS, Caller
from parapetai_agent.otel import openinference as oi
from parapetai_agent.policy.cost_tracker import CostTracker
from parapetai_agent.policy.cost_tracker import span_ids as _span_ids
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook
from parapetai_agent.policy.pricing import estimate_cost_usd_micros
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.scoped_data import agent_identity as agent_identity
from parapetai_agent.scoped_data import current_identity as current_identity
from parapetai_agent.scoped_data import effective_identity_claims, effective_identity_roles
from parapetai_agent.scoped_data import effective_principal as _effective_principal
from parapetai_agent.scoped_data import governed_identity as governed_identity
from parapetai_agent.scoped_data import identity_from_bearer_token as identity_from_bearer_token
from parapetai_agent.scoped_data import set_current_identity as set_current_identity

log = structlog.get_logger(__name__)

# Own instrumentation-scope tracer, same pattern as maf.py's module-level
# `_tracer = trace.get_tracer(__name__)` -- see that module's own comment on
# why this is safe to call at import time, before configure_otel() has run.
_tracer = trace.get_tracer(__name__)

# COST-TRACK-1 (docs/adr/0010): same cumulative token/cost tracking as
# maf.py, one process-wide instance -- trace_id is already globally unique,
# so sharing across every ParapetPlugin/Runner in this process is safe.
# Unlike maf.py, ADK gives us a real "invocation ended" hook
# (after_run_callback below), so this adapter calls end_trace() explicitly
# rather than relying solely on the tracker's own LRU bound.
_cost_tracker = CostTracker()

_PREVIEW_CHARS = 2000


def _log_content_enabled() -> bool:
    """Same PARAPETAI_OTEL_LOG_CONTENT gate as maf.py's own copy -- kept as
    an independent copy (not imported across the module boundary) since
    it's a tiny, pure env-var read with no state to drift, and importing it
    from maf.py would make this module depend on maf.py's own optional
    agent_framework import chain."""
    return os.environ.get("PARAPETAI_OTEL_LOG_CONTENT", "false").lower() == "true"


# LiteLlm prefixes whose spelling differs from the provider vocabulary the
# rest of this SDK uses (parapetai_agent.maf's _PROVIDER_BY_CLIENT_CLASS,
# providers.parsers). Deliberately tiny: anything not listed passes through
# as-is, so a LiteLlm provider we have never heard of becomes its own
# Resource:: value rather than being mapped onto a wrong one.
_LITELLM_PREFIX_ALIASES = {
    "azure_ai": "azure",
    "vertex_ai": "vertex",
}


def provider_for_request(llm_request: LlmRequest) -> str:
    """The provider actually being called, not the framework calling it.

    This returned a hardcoded "gemini" for every request, which was correct
    only for ADK's native path (Google's genai SDK). ADK also routes to other
    providers through its LiteLlm wrapper, and those decisions were labelled
    `gemini` too -- so a decision against Claude carried `Resource::"gemini"`
    and a rule written `resource == Resource::"anthropic"` silently never
    matched. A policy that does not fire and does not warn is the worst
    failure this engine can have, which is why this is no longer deferred.

    LiteLlm puts the provider in the model string as a `provider/model`
    prefix -- `anthropic/claude-haiku-4-5` -- and ADK passes that through to
    `LlmRequest.model` verbatim (verified against google-adk 2.7.1, not
    assumed). A model name with NO prefix is ADK's native path, which really
    is Gemini.

    Unknown prefixes pass through rather than collapsing to "unknown": LiteLlm
    supports well over a hundred providers and an unrecognised one is far
    better expressed as its own resource than silently merged into another's.
    """
    model = getattr(llm_request, "model", None) or ""
    prefix, separator, _ = str(model).partition("/")
    if not separator:
        return "gemini"
    prefix = prefix.strip().lower()
    return _LITELLM_PREFIX_ALIASES.get(prefix, prefix) or "gemini"


def _extract_texts(value: Any) -> list[str]:
    """Walks whatever shape GenerateContentConfig.system_instruction or an
    LlmRequest.contents list happens to be in (str / types.Content /
    types.Part / a list of any of those -- google-genai's own permissive
    union) and returns every text fragment found. Mirrors
    providers.parsers.GeminiParser's HTTP-JSON equivalent
    (systemInstruction.parts[].text + contents[].parts[].text) for the
    in-process path, reading google-genai's typed objects directly instead
    of parsing JSON."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, types.Content):
        texts: list[str] = []
        for part in value.parts or []:
            texts.extend(_extract_texts(part))
        return texts
    if isinstance(value, types.Part):
        return [value.text] if value.text else []
    if isinstance(value, (list, tuple)):
        texts = []
        for item in value:
            texts.extend(_extract_texts(item))
        return texts
    return []


def _declared_tools(config: types.GenerateContentConfig | None) -> list[str]:
    """config.tools[].function_declarations[].name -- the in-process
    equivalent of GeminiParser's body["tools"][i]["functionDeclarations"][j]
    ["name"] walk over the HTTP-JSON shape."""
    names: list[str] = []
    for tool in (config.tools if config else None) or []:
        for decl in getattr(tool, "function_declarations", None) or []:
            name = getattr(decl, "name", None)
            if name:
                names.append(str(name))
    return names


def _response_text(llm_response: LlmResponse) -> str:
    if llm_response.content is None:
        return ""
    return " ".join(_extract_texts(llm_response.content))


def _token_count_attributes(
    usage: types.GenerateContentResponseUsageMetadata | None,
) -> dict[str, int]:
    """llm.token_count.{prompt,completion,total} from google-genai's
    GenerateContentResponseUsageMetadata -- the ADK/genai equivalent of
    maf.py's own _token_count_attributes, different field names
    (prompt_token_count/candidates_token_count/total_token_count, not
    agent_framework's input_token_count/output_token_count/
    total_token_count), same OpenInference target keys."""
    if usage is None:
        return {}
    out: dict[str, int] = {}
    if usage.prompt_token_count is not None:
        out[oi.LLM_TOKEN_COUNT_PROMPT] = usage.prompt_token_count
    if usage.candidates_token_count is not None:
        out[oi.LLM_TOKEN_COUNT_COMPLETION] = usage.candidates_token_count
    if usage.total_token_count is not None:
        out[oi.LLM_TOKEN_COUNT_TOTAL] = usage.total_token_count
    return out


def _resolved_identity_claims(
    session_user_id: str | None, *, trust_session_user_id: bool
) -> dict[str, str]:
    """Ambient identity (governed_identity()/current_identity(), shared
    with every other framework integration via parapetai_agent.scoped_data)
    wins if set. Falling back to ADK's own Session.user_id is OPT-IN
    (trust_session_user_id=True, default False) -- see this module's own
    docstring's "Identity" section for why: Session.user_id is a plain,
    UNVERIFIED string (ADK's Runner.run_async() trusts whatever the caller
    passes, no authentication involved -- confirmed against runners.py's
    own _get_or_create_session()), and Cedar policies that check
    `context has identity_claims` (e.g. policies/30-identity.cedar's
    OrderViewer role gate) use that key specifically to distinguish "no
    identity asserted" from "identity asserted but missing a role". Letting
    an unverified value satisfy that check by default would make an
    identity-gated policy enforce a DIFFERENT default posture for ADK than
    for MAF, for reasons that have nothing to do with the policy itself --
    verified live: with this defaulted on, a caller that never logs in at
    all still got denied a role-gated tool call MAF would have allowed
    (nothing asserted, rule doesn't apply), because ADK requires user_id
    unconditionally where MAF's identity is fully optional. Set
    trust_session_user_id=True only when your own deployment's user_id
    values are genuinely trustworthy (e.g. YOU set them from a verified
    source before calling run_async(), not just because ADK required
    something non-empty)."""
    ambient = effective_identity_claims(None)
    if ambient:
        return ambient
    if trust_session_user_id and session_user_id:
        return {"sub": str(session_user_id)}
    return {}


def _resolved_identity_roles() -> list[str]:
    """ADK's Session carries no role concept, so the only source here is
    ambient identity -- see _resolved_identity_claims()."""
    return effective_identity_roles(None)


def _denied_llm_response(decision: Decision) -> LlmResponse:
    """The synthetic LlmResponse a Cedar deny substitutes for the real
    model call/response -- ADK's own "early exit"/"replace the response"
    mechanism (BasePlugin.before_model_callback/after_model_callback's own
    docstrings), not a raised exception (contrast GovernanceDenied, which
    parapetai_agent.maf raises because that's what MAF's own hook requires
    to reliably stop the call -- see that module's "Enforcement asymmetry"
    section for why the two frameworks need different mechanisms here).
    Carries the same "GOVERNANCE_DENIED: <reason>" text prefix maf.py's
    tool-call denial uses, so a caller pattern-matching for that prefix
    works across both frameworks."""
    return LlmResponse(
        error_code="governance_denied",
        error_message=decision.reason,
        content=types.Content(
            role="model", parts=[types.Part(text=f"GOVERNANCE_DENIED: {decision.reason}")]
        ),
        turn_complete=True,
    )


def _redact_all(value: Any) -> Any:
    """The one built-in ALTER transform -- a placeholder, not a real
    redaction strategy, same caveat as maf.py's own copy: this repo has no
    PII/secrets detector to ship. Registered under "redact_all" so tests
    and a first bundle have something real to name; a production
    deployment should register its own via
    build_plugin(alter_transforms=...), which OVERRIDES this entry for the
    same name rather than layering on top of it."""
    if isinstance(value, LlmResponse):
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="[REDACTED BY POLICY]")]),
            turn_complete=True,
        )
    return "[REDACTED BY POLICY]"


DEFAULT_ALTER_TRANSFORMS: dict[str, Callable[[Any], Any]] = {"redact_all": _redact_all}


def _parent_context_from_span_context(span_context: SpanContext | None) -> Any:
    """Reconstructs a usable parent Context from a completed/still-open
    span's SpanContext alone -- same technique maf.py's
    _parent_context_from_correlation() uses, so a tool_call span can be
    explicitly linked as a child of the model_call span that triggered it
    regardless of whether the two spans are opened/closed from the same
    callback invocation (they are not: before_model_callback and
    before_tool_callback are separate calls into this plugin)."""
    if span_context is None:
        return None
    return trace.set_span_in_context(NonRecordingSpan(span_context))


@dataclass(slots=True)
class _ModelCorrelation:
    """What before_model_callback captured about the request driving the
    current invocation, kept alive (keyed by invocation_id) until
    after_model_callback's final (non-partial) chunk closes it out --
    the ADK equivalent of maf.py's _ChatCorrelation, simpler because ADK's
    own Context/ToolContext already carries a shared invocation_id linking
    a model call to the tool calls it triggers (no sibling-vs-nested span
    workaround needed here, unlike MAF -- see that module's own module
    docstring)."""

    model: str | None = None
    # The provider the model call actually went to. Carried here because the
    # tool callbacks see only a ToolContext -- there is no LlmRequest to read
    # it off -- and a tool call belongs to whichever provider's model asked
    # for it. Defaults to gemini for the same reason provider_for_request
    # does: a bare model name is ADK's native path.
    provider: str = "gemini"
    span: Span | None = None
    span_context: SpanContext | None = None
    principal: str = ""
    identity_claims: dict[str, str] = field(default_factory=dict)
    identity_roles: list[str] = field(default_factory=list)
    stream: bool = False
    partial_text: list[str] = field(default_factory=list)


def _tool_call_cost_ids(
    correlation: _ModelCorrelation, tool_span: Span
) -> tuple[str | None, str | None]:
    """(trace_id, scope_id) for a tool_call's COST_TRACK-1 context -- shared
    by before_tool_callback and after_tool_callback so the two derive the
    exact same keys rather than each re-implementing the correlated/
    fallback logic. scope_id is the triggering model_call's own span id
    (correlation.span_context) when one exists; falls back to the
    tool_call's own span id only when tool_context carried no correlated
    model_call (correlation is a bare fallback _ModelCorrelation())."""
    correlated = _span_ids(correlation.span_context) if correlation.span_context else None
    own = _span_ids(tool_span.get_span_context())
    trace_id, own_span_id = own if own else (None, None)
    scope_id = correlated[1] if correlated else own_span_id
    return trace_id, scope_id


class ParapetPlugin(BasePlugin):
    """Cedar governance for every model_call/tool_call an ADK Runner
    drives -- registered once via Runner(plugins=[...]), covering every
    agent in that Runner's app. See this module's own docstring for why
    Runner (via a Plugin), not Agent, is ADK's governable seam, and for
    the streaming/identity design notes."""

    def __init__(
        self,
        engine: PolicyEngine,
        caller: Caller,
        *,
        alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
        content_checks: ContentCheckConfig | None = None,
        plugin_name: str = "parapetai",
        trust_session_user_id: bool = False,
    ) -> None:
        super().__init__(name=plugin_name)
        self.engine = engine
        self.caller = caller
        self.hook = GovernanceHook(engine, caller, on_decision=_audit)
        self._alter_transforms = {**DEFAULT_ALTER_TRANSFORMS, **(alter_transforms or {})}
        # Tier-2 content checks (parapetai_agent/content_checks.py), pre-call
        # only -- same scope as maf.py's own. None means "no tier-2
        # config", a harmless no-op below. Groundedness/response-judge
        # (QUAL-1, post-call) are NOT wired here yet -- see this module's
        # "Known gaps" section.
        self._content_checks = content_checks
        # See _resolved_identity_claims()'s own docstring for why this is
        # opt-in, default False: Session.user_id is unverified, and
        # defaulting this on would silently make identity-gated Cedar
        # policies stricter for ADK than for MAF by default.
        self._trust_session_user_id = trust_session_user_id
        self._model_correlations: dict[str, _ModelCorrelation] = {}
        self._tool_spans: dict[tuple[str, str], Span] = {}
        # Provider per invocation, kept SEPARATELY from _model_correlations
        # because after_model_callback pops the correlation as soon as the
        # model responds -- which is BEFORE the tool calls that response
        # asked for. Reading the provider off the correlation therefore
        # always hit its default on the tool path, which is how the
        # hardcoded "gemini" survived its first removal. Cleaned up in
        # after_run_callback, so a long-running server does not grow one
        # entry per invocation.
        self._provider_by_invocation: dict[str, str] = {}

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        invocation_id = callback_context.invocation_id
        run_config = callback_context.run_config
        stream = run_config is not None and run_config.streaming_mode == StreamingMode.SSE
        identity_claims = _resolved_identity_claims(
            callback_context.user_id, trust_session_user_id=self._trust_session_user_id
        )
        identity_roles = _resolved_identity_roles()
        principal = _effective_principal(self.caller)

        span = _tracer.start_span("parapetai.model_call")
        correlation = _ModelCorrelation(
            model=llm_request.model,
            span=span,
            span_context=span.get_span_context(),
            principal=principal,
            identity_claims=identity_claims,
            identity_roles=identity_roles,
            stream=stream,
        )
        correlation.provider = provider_for_request(llm_request)
        self._provider_by_invocation[invocation_id] = correlation.provider
        self._model_correlations[invocation_id] = correlation

        texts = _extract_texts(
            llm_request.config.system_instruction if llm_request.config else None
        )
        texts += _extract_texts(llm_request.contents)
        declared_tools = _declared_tools(llm_request.config)
        snapshot = Snapshot(
            provider=provider_for_request(llm_request),
            endpoint="in-process:adk:model_call",
            model=llm_request.model,
            parsed=True,
            messages_preview=" ".join(texts)[:_PREVIEW_CHARS],
            declared_tools=declared_tools,
            stream=stream,
            identity_claims=identity_claims,
            identity_roles=identity_roles,
        )
        _set_oi_attributes(
            span,
            {
                oi.SPAN_KIND_ATTR: oi.SpanKind.LLM,
                oi.LLM_MODEL_NAME: snapshot.model,
                oi.LLM_PROVIDER: snapshot.provider,
                oi.LLM_TOOLS: declared_tools,
                oi.LLM_INVOCATION_PARAMETERS: json.dumps({"stream": stream}),
            },
        )
        if _log_content_enabled():
            _set_oi_attributes(span, {oi.LLM_INPUT_MESSAGES: texts})

        # Tier-2 scanners run BEFORE the Cedar decision -- see
        # parapetai_agent.content_checks's module docstring / maf.py's own
        # ParapetChatMiddleware for why a scanner failure must deny here,
        # never let Cedar see an absent context key and silently treat a
        # configured check as a no-op.
        content_result = self._content_checks.evaluate(snapshot) if self._content_checks else None
        if content_result is not None and content_result.errors:
            denial = _content_check_failure_decision(
                self.engine.status["generation"], content_result.errors
            )
            span.set_status(Status(StatusCode.ERROR, denial.reason))
            span.end()
            self._model_correlations.pop(invocation_id, None)
            return _denied_llm_response(denial)
        extra_context = content_result.context if content_result else {}
        # COST-TRACK-1: cumulative totals so far (not including this call --
        # its usage isn't known until after_model_callback below). scope_id
        # is this model_call's OWN span id: the "turn" scope (this call plus
        # whatever tool_call(s) it triggers) is keyed off it, same
        # definition maf.py uses.
        ids = _span_ids(span.get_span_context())
        trace_id, scope_id = ids if ids else (None, None)
        extra_context = {
            **extra_context,
            **_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
        }
        pre = self.hook.evaluate(
            snapshot=snapshot, stage="pre", principal=principal, extra_context=extra_context
        )
        if not pre.decision.allowed:
            span.set_status(Status(StatusCode.ERROR, pre.decision.reason))
            span.end()
            self._model_correlations.pop(invocation_id, None)
            return _denied_llm_response(pre.decision)
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        invocation_id = callback_context.invocation_id
        correlation = self._model_correlations.get(invocation_id)
        if correlation is None:
            # No matching before_model_callback -- either it already denied
            # and cleaned up (see above), or this fired for a call this
            # plugin never saw. Nothing of ours to evaluate against.
            return None

        text = _response_text(llm_response)
        if text:
            correlation.partial_text.append(text)

        if llm_response.partial:
            # Relay unmodified -- see this module's own docstring's
            # "Streaming" section for why post-call evaluation waits for
            # the final, non-partial chunk instead of running per chunk.
            return None

        span = correlation.span
        accumulated = "".join(correlation.partial_text)
        ids = _span_ids(correlation.span_context) if correlation.span_context else None
        trace_id, scope_id = ids if ids else (None, None)
        try:
            token_attrs: dict[str, int] = {}
            if span is not None:
                token_attrs = _token_count_attributes(llm_response.usage_metadata)
                _set_oi_attributes(span, token_attrs)
                if _log_content_enabled():
                    _set_oi_attributes(span, {oi.LLM_OUTPUT_MESSAGES: accumulated})
            # COST-TRACK-1: this call's usage is known now -- record it
            # against the SAME (trace_id, scope_id) the pre-call context_for()
            # above read from, so the next call in this trace/turn sees an
            # up-to-date cumulative total. See maf.py's identical comment for
            # why an unpriced model still credits tokens (cost_usd_micros
            # stays 0 for it, never skipped -- an unpriced call must still
            # count toward a TOKEN budget).
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

            response_snapshot = Snapshot(
                provider=correlation.provider,
                endpoint="in-process:adk:model_call",
                model=correlation.model,
                parsed=True,
                stream=correlation.stream,
                response_preview=accumulated[:_PREVIEW_CHARS],
                identity_claims=correlation.identity_claims,
                identity_roles=correlation.identity_roles,
            )
            # Seeded with the JUST-updated cumulative totals so a post-stage
            # policy (e.g. ALTER once a turn crosses a budget) sees this
            # call's own usage, not the pre-call snapshot.
            post = self.hook.evaluate(
                snapshot=response_snapshot,
                stage="post",
                principal=correlation.principal,
                extra_context=_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
            )
            if not post.decision.allowed:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR, post.decision.reason))
                return _denied_llm_response(post.decision)
            if post.alter_with is not None:
                transform = self._alter_transforms.get(post.alter_with)
                if transform is None:
                    denial = _unresolved_alter_decision(
                        post.decision.policy_generation, post.alter_with
                    )
                    if span is not None:
                        span.set_status(Status(StatusCode.ERROR, denial.reason))
                    return _denied_llm_response(denial)
                return transform(llm_response)  # type: ignore[no-any-return]
            return None
        finally:
            if span is not None:
                span.end()
            self._model_correlations.pop(invocation_id, None)

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        invocation_id = tool_context.invocation_id
        call_key = (invocation_id, tool_context.function_call_id or "")
        correlation = self._model_correlations.get(invocation_id) or _ModelCorrelation(
            principal=_effective_principal(self.caller),
            identity_claims=_resolved_identity_claims(
                tool_context.user_id, trust_session_user_id=self._trust_session_user_id
            ),
            identity_roles=_resolved_identity_roles(),
            provider=self._provider_by_invocation.get(invocation_id, "gemini"),
        )

        parent_ctx = _parent_context_from_span_context(correlation.span_context)
        span = _tracer.start_span("parapetai.tool_call", context=parent_ctx)
        self._tool_spans[call_key] = span
        _set_oi_attributes(span, {oi.SPAN_KIND_ATTR: oi.SpanKind.TOOL, oi.TOOL_NAME: tool.name})
        if _log_content_enabled():
            _set_oi_attributes(span, {oi.TOOL_PARAMETERS: json.dumps(tool_args, default=str)})

        snapshot = Snapshot(
            provider=correlation.provider,
            endpoint="in-process:adk:tool_call",
            model=correlation.model,
            parsed=True,
            tool_name=tool.name,
            tool_args=dict(tool_args),
            identity_claims=correlation.identity_claims,
            identity_roles=correlation.identity_roles,
        )
        # COST-TRACK-1: scope_id is the TRIGGERING model_call's own span id
        # (via `correlation`, the SAME link this tool_call span was parented
        # to above) so this tool call accumulates into the SAME "turn" total
        # as the model_call that requested it -- falls back to this
        # tool_call's own span id only when there was no correlated
        # model_call at all (the `or _ModelCorrelation(...)` default above).
        trace_id, scope_id = _tool_call_cost_ids(correlation, span)
        pre = self.hook.evaluate(
            snapshot=snapshot,
            stage="pre",
            principal=correlation.principal,
            extra_context=_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
        )
        if not pre.decision.allowed:
            span.set_status(Status(StatusCode.ERROR, pre.decision.reason))
            _record_tool_denial(pre.decision.reason)
            return {"error": f"GOVERNANCE_DENIED: {pre.decision.reason}"}
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        invocation_id = tool_context.invocation_id
        call_key = (invocation_id, tool_context.function_call_id or "")
        span = self._tool_spans.pop(call_key, None)
        correlation = self._model_correlations.get(invocation_id) or _ModelCorrelation(
            principal=_effective_principal(self.caller),
            identity_claims=_resolved_identity_claims(
                tool_context.user_id, trust_session_user_id=self._trust_session_user_id
            ),
            identity_roles=_resolved_identity_roles(),
            provider=self._provider_by_invocation.get(invocation_id, "gemini"),
        )
        try:
            if span is not None and _log_content_enabled():
                _set_oi_attributes(span, {oi.OUTPUT_VALUE: json.dumps(result, default=str)})
            response_snapshot = Snapshot(
                provider=correlation.provider,
                endpoint="in-process:adk:tool_call",
                model=correlation.model,
                parsed=True,
                tool_name=tool.name,
                tool_result_preview=json.dumps(result, default=str)[:_PREVIEW_CHARS],
                identity_claims=correlation.identity_claims,
                identity_roles=correlation.identity_roles,
            )
            trace_id, scope_id = _tool_call_cost_ids(correlation, span) if span else (None, None)
            post = self.hook.evaluate(
                snapshot=response_snapshot,
                stage="post",
                principal=correlation.principal,
                extra_context=_cost_tracker.context_for(trace_id=trace_id, scope_id=scope_id),
            )
            if not post.decision.allowed:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR, post.decision.reason))
                _record_tool_denial(post.decision.reason)
                return {"error": f"GOVERNANCE_DENIED: {post.decision.reason}"}
            if post.alter_with is not None:
                transform = self._alter_transforms.get(post.alter_with)
                if transform is None:
                    denial = _unresolved_alter_decision(
                        post.decision.policy_generation, post.alter_with
                    )
                    if span is not None:
                        span.set_status(Status(StatusCode.ERROR, denial.reason))
                    _record_tool_denial(denial.reason)
                    return {"error": f"GOVERNANCE_DENIED: {denial.reason}"}
                return transform(result)  # type: ignore[no-any-return]
            return None
        finally:
            if span is not None:
                span.end()

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        """Defensive cleanup for an invocation whose final after_model_callback
        chunk never arrived (e.g. the underlying call errored before
        producing one) -- without this, that invocation's correlation entry
        and open span would leak for the lifetime of the process in a
        long-running server."""
        self._provider_by_invocation.pop(invocation_context.invocation_id, None)
        correlation = self._model_correlations.pop(invocation_context.invocation_id, None)
        if correlation is not None and correlation.span is not None:
            correlation.span.end()


@dataclass(slots=True)
class _PluginRegistryEntry:
    engine: PolicyEngine
    plugin: ParapetPlugin
    stop_event: threading.Event | None  # None when no background sync was started
    thread: threading.Thread | None = None  # the background poller, so reset can join it


_plugin_registry: dict[tuple[str, str, str, str, str], _PluginRegistryEntry] = {}
_plugin_registry_lock = threading.Lock()


def reset_plugin_registry() -> None:
    """Test-only: stops every background sync thread build_plugin() has
    started in this process and forgets all cached plugins, so the next
    build_plugin() call for a previously-seen identity does real
    construction again instead of returning a stale, previous test's
    engine/plugin. Real callers never need this. Same shape as
    maf.reset_middleware_registry() -- see that function's own docstring
    for why the JOIN (not just the stop_event signal) matters."""
    with _plugin_registry_lock:
        entries = list(_plugin_registry.values())
        _plugin_registry.clear()
    for entry in entries:
        if entry.stop_event is not None:
            entry.stop_event.set()
    for entry in entries:
        if entry.thread is not None:
            entry.thread.join(timeout=5.0)


def build_plugin(
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
    trust_session_user_id: bool = False,
) -> ParapetPlugin:
    """One PolicyEngine, one Caller, one ParapetPlugin -- the ADK
    equivalent of parapetai_agent.maf.build_middleware(), same kwarg
    surface, same semantics for every parameter (policy resolution,
    control-plane pull, Ed25519 PEP identity, OTel auto-wiring, idempotent
    per-identity caching) -- see that function's own extensive docstring
    for the full story on each; it is not repeated here since none of it
    is MAF-specific (control_plane/pep_identity/governance_runtime are
    already framework-agnostic, confirmed during this module's own
    construction).

    trust_session_user_id is ADK-specific (MAF has no equivalent, since
    MAF's AgentSession carries no user_id at all): default False, meaning
    ADK's own Session.user_id -- a plain, UNVERIFIED string every
    run_async() call must supply, but never authenticated by ADK itself --
    does NOT flow into Cedar's identity_claims. See
    ParapetPlugin.__init__()/_resolved_identity_claims()'s own docstrings
    for exactly why defaulting this on is unsafe: it would make
    identity-gated Cedar policies (e.g. policies/30-identity.cedar)
    silently stricter for ADK than for MAF, since ADK requires user_id
    unconditionally where MAF's identity is fully optional. Set this True
    only when your own deployment sets user_id from a source you already
    trust. NOT part of the identity-registry cache key below, same as
    alter_transforms -- affects construction, not identity.

    Most callers should use GovernedRunner (below) instead of calling this
    directly, same relationship build_middleware() has to GovernedAgent.
    """
    if local_log_dir is not None:
        configure_rotating_audit_log(local_log_dir, console=console)

    resolved_control_plane_url = control_plane_url or os.environ.get("PARAPETAI_CONTROL_PLANE_URL")
    resolved_agent_secret = agent_secret or os.environ.get("PARAPETAI_AGENT_SECRET")
    control_plane_configured = bool(resolved_control_plane_url and resolved_agent_secret)

    if control_plane_configured and not otel_configured():
        # See build_middleware()'s own docstring's "OpenTelemetry is wired
        # up automatically too" paragraph -- otel_configured() is the
        # SHARED, process-wide check (parapetai_agent.governance_runtime),
        # so this correctly yields to an embedder's own earlier
        # configure_otel() call OR to parapetai_agent.maf.build_middleware()
        # already having done this in the same process.
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

    with _plugin_registry_lock:
        cached = _plugin_registry.get(key)
        if cached is not None:
            return cached.plugin

        resolved_pep_key_path = (
            (Path(pep_key_path) if pep_key_path else pep_identity.default_key_path())
            if persist_pep_key
            else None
        )
        # Tier-2 content-check scanner config -- ALWAYS constructed, never a
        # caller-supplied opt-in, same reasoning as build_middleware()'s own
        # copy: any SDK version new enough to have this module at all
        # enforces whatever content_checks.json its bundle carries.
        content_checks = ContentCheckConfig()

        def _load_bundle_configs(files: dict[str, str]) -> None:
            content_checks.load_from_bundle(files)

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
        plugin = ParapetPlugin(
            engine,
            caller,
            alter_transforms=alter_transforms,
            content_checks=content_checks,
            trust_session_user_id=trust_session_user_id,
        )
        _plugin_registry[key] = _PluginRegistryEntry(engine, plugin, stop_event, poll_thread)
        return plugin


class GovernedRunner(Runner):
    """google.adk.runners.Runner with Cedar governance wired in
    automatically -- a drop-in replacement for Runner(...) that removes
    the one real gap in this module's design: build_plugin() +
    plugins=[plugin] is genuinely the ENTIRE integration (two lines), but
    it's opt-in per Runner(...) call site. Forget the plugins= kwarg and
    there is zero enforcement, silently. Swapping the Runner import for
    GovernedRunner removes that failure mode, same rationale as
    parapetai_agent.maf.GovernedAgent -- see that class's own docstring
    for why this is a visible, one-line import swap rather than a
    process-wide monkeypatch.

    Unlike GovernedAgent, every Runner(...) constructor parameter is
    keyword-only (confirmed against google-adk 2.7.1's own Runner.__init__
    signature -- there are no positional parameters to forward), so this
    class does too.

    Any plugins passed explicitly via plugins=[...] (or already present on
    an app=App(..., plugins=[...])) run ALONGSIDE the governance plugin
    (ADK invokes every registered plugin's matching callback for a given
    hook point; there is no single-plugin-wins semantics the way MAF's
    middleware chain has an ordering).

    Confirmed against google-adk 2.7's own Runner._resolve_app(): passing
    BOTH app= and plugins= raises ValueError ("plugins should not be
    provided ... provide it in the app instead") -- plugins= itself is
    deprecated as of 2.7 in favor of App(plugins=[...]). This class
    therefore branches on which construction style the caller used: if
    app= is present, the governance plugin is appended to app.plugins
    in place (App.plugins is a plain mutable pydantic field, confirmed);
    otherwise it goes through the deprecated plugins= kwarg, same as
    before. Both paths are exercised in tests/test_adk.py.

    agent_id is optional -- see build_plugin()/build_middleware()'s
    docstring for the Agent::"anonymous" fallback. trust_session_user_id
    (default False) is ADK-specific -- see build_plugin()'s own docstring
    for why ADK's Session.user_id being unverified means it must be an
    explicit opt-in, not a default, unlike MAF (which has no equivalent
    ambient user_id to even opt into). EVERYTHING else is
    optional too -- the minimal call is
    `GovernedRunner(agent=..., app_name=..., session_service=...)`,
    nothing more, and it enforces real (if generic) Cedar policy from the
    moment it's constructed, using the policy set bundled in
    parapetai-agent.
    """

    def __init__(
        self,
        *,
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
        trust_session_user_id: bool = False,
        **kwargs: Any,
    ) -> None:
        plugin = build_plugin(
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
            trust_session_user_id=trust_session_user_id,
        )
        app = kwargs.get("app")
        if app is not None:
            app.plugins = [plugin, *(app.plugins or [])]
        else:
            kwargs["plugins"] = [plugin, *(kwargs.get("plugins") or [])]
        super().__init__(**kwargs)


class InMemoryGovernedRunner(GovernedRunner):
    """GovernedRunner + google.adk.runners.InMemoryRunner's own convenience
    defaults, combined -- for the common case of reaching for `Runner`'s
    simplest possible construction with no real session/artifact/memory
    backend. Real ADK samples commonly use InMemoryRunner(agent=...,
    plugins=[...]) directly, not bare Runner(session_service=...,
    artifact_service=..., ...) (confirmed against several samples under
    google/adk-samples, e.g. safety-plugins/main.py); GovernedRunner alone
    does not mirror that convenience, since it subclasses Runner directly,
    not InMemoryRunner -- a caller who reflexively swaps InMemoryRunner
    for GovernedRunner and forgets session_service= gets a real, loud
    TypeError, not a silent gap, but it's real, avoidable friction for
    exactly the construction shape most first-run samples use. Use this
    instead of GovernedRunner whenever your own code would otherwise reach
    for InMemoryRunner.

    Confirmed against google-adk 2.7's own InMemoryRunner.__init__: it
    pre-fills session_service/artifact_service/memory_service with fresh
    InMemory* instances and defaults app_name to "InMemoryRunner" (only
    when neither app_name nor app is given), then forwards to
    Runner.__init__ unchanged -- this class does the identical pre-fill,
    then forwards to GovernedRunner.__init__ (not Runner.__init__
    directly) so the SAME governance kwargs (policy_dir, agent_id,
    control_plane_url, trust_session_user_id, ...) still work exactly as
    they do on GovernedRunner itself.

    Explicit artifact_service=/memory_service=/session_service= kwargs, if
    passed, override these defaults (via dict.setdefault semantics) rather
    than being silently replaced -- e.g. a caller that wants the in-memory
    convenience for artifacts/memory but a REAL, persistent
    session_service can still pass just that one kwarg through."""

    def __init__(
        self,
        agent: AdkBaseAgent | None = None,
        *,
        app_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        if kwargs.get("app") is None and app_name is None:
            app_name = "InMemoryRunner"
        kwargs.setdefault("artifact_service", InMemoryArtifactService())
        kwargs.setdefault("memory_service", InMemoryMemoryService())
        kwargs.setdefault("session_service", InMemorySessionService())
        super().__init__(agent=agent, app_name=app_name, **kwargs)
