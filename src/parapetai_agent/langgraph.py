"""LangGraph / LangChain integration: Cedar governance wired into
`langchain.agents.create_agent(..., middleware=[...])` via a real
`AgentMiddleware` -- verified live (see class docstring below) to support
genuine BLOCKING of a model or tool call before it executes, the same
class of guarantee `parapetai_agent.maf`'s `ParapetChatMiddleware`/
`ParapetFunctionMiddleware` give MAF, via `AgentMiddleware.wrap_model_call`/
`wrap_tool_call` receiving a `handler` callable this middleware controls
whether to invoke at all.

Targets `langchain.agents.create_agent` (the current, non-deprecated
construction API as of `langchain>=1.3`), not the older, deprecated
`langgraph.prebuilt.create_react_agent` -- confirmed live that
`create_react_agent` predates `middleware=` support and is unsuitable as
this module's target for exactly that reason. This is why this extra
depends on the full `langchain` package, not just `langgraph`/
`langchain-core` (see pyproject.toml's `langgraph` extra) -- `AgentMiddleware`
lives in `langchain.agents.middleware`, not in `langgraph` itself.

Same governance-kwarg surface, same shared plumbing, as
`parapetai_agent.maf.build_middleware()`/`parapetai_agent.adk.build_plugin()`:
`governance_runtime.py`'s `resolve_policy_source`/`audit`/`configure_otel`/
`configure_rotating_audit_log`/`track_tool_denials` are reused unchanged --
this module's own-specific work is exactly the two things
`policy/hooks.py`'s own module docstring says a new adapter's work should
be: building a `Snapshot` from LangChain's `ModelRequest`/`ToolCallRequest`
objects, and calling `GovernanceHook.evaluate()` at LangChain's own
`wrap_model_call`/`wrap_tool_call` hook points.

KNOWN GAPS, deliberately deferred rather than silently half-built --
tracked here so a caller doesn't assume parity with `maf.py`/`adk.py` that
doesn't exist yet:

- **No tier-2 content-checks/groundedness/judge scanning, no ALTER
  support.** `alter_transforms=` is not a parameter here (unlike
  `build_middleware()`/`build_plugin()`) -- accepting it and silently
  doing nothing with it would be worse than not accepting it at all.
  `check_input`/`check_output`-equivalent Cedar gating (`model_call`/
  `post` stages) IS implemented; the additional PII/injection/groundedness/
  judge scanners a control-plane bundle can carry are not wired in yet.
- **No per-call OTel span with OpenInference attributes** (token counts,
  model name, `parapetai.model_call` span). Decisions still reach OTel as
  LogRecords via `governance_runtime.audit()` (the same sink MAF/ADK
  use) -- what's missing is the additional per-call *span*, not decision
  observability entirely. Cumulative cost/token tracking (below) does NOT
  depend on this gap being closed -- it derives its own trace/span ids
  from `before_agent`/`after_agent`/`wrap_model_call` rather than from an
  OTel `SpanContext` the way `maf.py`/`adk.py` do, since no such
  `SpanContext` exists here yet.
- **Streaming has not been verified against a live streaming
  `.astream()`/`.stream()` call** -- treat as unverified, not assumed
  either way, the same caution `adk.py`'s own docstring applies to its own
  streaming claim before it was verified.
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from parapetai_agent import pep_identity
from parapetai_agent.control_plane import bootstrap_engine
from parapetai_agent.governance_runtime import GovernanceDenied as GovernanceDenied
from parapetai_agent.governance_runtime import audit as _audit
from parapetai_agent.governance_runtime import configure_otel as configure_otel
from parapetai_agent.governance_runtime import (
    configure_rotating_audit_log as configure_rotating_audit_log,
)
from parapetai_agent.governance_runtime import flush_otel as flush_otel
from parapetai_agent.governance_runtime import installed_version as _installed_version
from parapetai_agent.governance_runtime import otel_configured
from parapetai_agent.governance_runtime import record_tool_denial as _record_tool_denial
from parapetai_agent.governance_runtime import (
    resolve_local_output_settings as _resolve_local_output_settings,
)
from parapetai_agent.governance_runtime import resolve_policy_source as _resolve_policy_source
from parapetai_agent.governance_runtime import track_tool_denials as track_tool_denials
from parapetai_agent.identity import ANONYMOUS, Caller
from parapetai_agent.policy.cost_tracker import CostTracker
from parapetai_agent.policy.cost_tracker import new_span_id as _new_span_id
from parapetai_agent.policy.cost_tracker import new_trace_id as _new_trace_id
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook
from parapetai_agent.policy.pricing import estimate_cost_usd_micros
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.scoped_data import agent_identity as agent_identity
from parapetai_agent.scoped_data import current_identity as current_identity
from parapetai_agent.scoped_data import effective_identity_claims as _effective_identity_claims
from parapetai_agent.scoped_data import effective_identity_roles as _effective_identity_roles
from parapetai_agent.scoped_data import effective_principal as _effective_principal
from parapetai_agent.scoped_data import governed_identity as governed_identity
from parapetai_agent.scoped_data import identity_from_bearer_token as identity_from_bearer_token
from parapetai_agent.scoped_data import set_current_identity as set_current_identity
from parapetai_agent.vendor_calls import resolve_vendor_call as _resolve_vendor_call
from parapetai_agent.vendor_calls import (
    resolve_vendor_call_from_metadata as _resolve_vendor_call_from_metadata,
)

log = structlog.get_logger(__name__)

_PREVIEW_CHARS = 4000

# One CostTracker per process, same pattern as maf.py/adk.py's own
# module-level _cost_tracker -- trace_id is globally unique (random hex),
# so sharing is safe and avoids a second instance per ParapetAgentMiddleware.
_cost_tracker = CostTracker()

# TRACE = one before_agent/after_agent bracket (the whole agent.invoke()/
# ainvoke() run). SPAN = one model_call turn plus whatever tool_call(s) it
# triggers. Unlike maf.py/adk.py, there is no OTel SpanContext to derive
# these from yet (see module docstring's Known Gaps), so they're generated
# here and threaded through contextvars -- the same mechanism, and the
# same reason, as maf.py's _current_chat: correctly isolated per asyncio
# task under concurrent agent.invoke() calls sharing one middleware
# instance, where a plain instance attribute would leak across concurrent
# runs. `_current_span_id` is set once per wrap_model_call/awrap_model_call
# and deliberately LEFT SET rather than reset in a finally -- confirmed
# live (a scripted multi-turn tool-calling run) that before_agent,
# wrap_model_call, wrap_tool_call, wrap_model_call, after_agent fire as
# SEPARATE, SEQUENTIAL steps, never nested, so a reset-on-return would
# clear it before the tool_call that needs it ever reads it -- exactly the
# bug _current_chat's own docstring in maf.py describes hitting once.
_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_langgraph_current_trace_id", default=None
)
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_langgraph_current_span_id", default=None
)


def _usage_tokens(response: ModelResponse) -> tuple[int, int, int]:
    """(prompt_tokens, completion_tokens, total_tokens) from the last
    message in the response that carries a populated `usage_metadata` --
    not every provider populates it (confirmed: langchain_core's own
    GenericFakeChatModel doesn't), and an absent value must read as "no
    usage known" (all zeros), never raise."""
    for message in reversed(response.result):
        usage = getattr(message, "usage_metadata", None)
        if usage:
            prompt = int(usage.get("input_tokens") or 0)
            completion = int(usage.get("output_tokens") or 0)
            total = int(usage.get("total_tokens") or 0) or (prompt + completion)
            return prompt, completion, total
    return 0, 0, 0


@dataclass(slots=True)
class _ModelCallScope:
    """Cost-tracking correlation for one model_call turn -- trace_id
    persists for the whole agent run (from before_agent, or a fresh
    one-off if before_agent never fired -- see its own docstring); span_id
    is fresh per turn and shared with whatever tool_call(s) this turn goes
    on to trigger."""

    trace_id: str
    span_id: str
    model: str | None


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some providers return content BLOCKS, not a flat string
        return " ".join(str(part) for part in content if isinstance(part, str))
    return ""


class ParapetAgentMiddleware(AgentMiddleware):
    """Model-call and tool-call governance for `langchain.agents.
    create_agent(..., middleware=[...])`. Runs a real Cedar `model_call`
    decision before the model is invoked and a `post` decision on its
    response, plus a `tool_call` decision before each tool actually runs --
    all THREE stages, unlike this module's own generic fallback
    (`parapetai_agent.Governor.tool`, which is tool-call only).

    Verified live, not assumed: `wrap_model_call`/`wrap_tool_call` receive
    a `handler` callable this middleware explicitly chooses whether to
    invoke -- raising `GovernanceDenied` before calling `handler(request)`
    genuinely prevents the model/tool call from ever executing, propagating
    as a real exception through `agent.invoke()`/`agent.ainvoke()`. This
    was confirmed against `langchain==1.3.18`/`langgraph==1.2.11` by
    actually constructing an agent and denying a real tool call, not
    inferred from the type signature alone."""

    def __init__(
        self, engine: PolicyEngine, caller: Caller, *, vendor_scoped_resources: bool = False
    ) -> None:
        self.engine = engine
        self.caller = caller
        self.hook = GovernanceHook(
            engine, caller, on_decision=_audit, vendor_scoped_resources=vendor_scoped_resources
        )

    # ------------------------------------------------------------------ #
    # trace boundary -- cumulative cost/token tracking scope only (no
    # Cedar decision here). See docs/reference/cost-tracking.md.
    # ------------------------------------------------------------------ #

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        _current_trace_id.set(_new_trace_id())
        return None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        _current_trace_id.set(_new_trace_id())
        return None

    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._end_trace()
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._end_trace()
        return None

    def _end_trace(self) -> None:
        # Unlike maf.py/adk.py (which rely purely on CostTracker's LRU to
        # eventually bound memory, since neither has a reliable
        # "trace ended" callback -- see cost_tracker.py's own docstring),
        # before_agent/after_agent genuinely DO bracket one whole run here,
        # so calling end_trace() as a courtesy is the honest default, not
        # just an optimization.
        trace_id = _current_trace_id.get()
        if trace_id is not None:
            _cost_tracker.end_trace(trace_id)
        _current_trace_id.set(None)
        _current_span_id.set(None)

    # ------------------------------------------------------------------ #
    # model call -- pre (Cedar model_call) then post (Cedar post)
    # ------------------------------------------------------------------ #

    def _pre_model_snapshot(
        self, request: ModelRequest
    ) -> tuple[Snapshot, str, dict[str, str], list[str], _ModelCallScope, dict[str, int]]:
        texts = [_message_text(m) for m in request.messages]
        declared_tools = [
            getattr(t, "name", None) for t in request.tools if getattr(t, "name", None)
        ]
        identity_claims = _effective_identity_claims(None)
        identity_roles = _effective_identity_roles(None)
        model_name = getattr(request.model, "model_name", None) or getattr(
            request.model, "model", None
        )
        snapshot = Snapshot(
            provider="langgraph",
            endpoint="in-process:langgraph:model_call",
            model=model_name,
            parsed=True,
            messages_preview=" ".join(t for t in texts if t)[:_PREVIEW_CHARS],
            declared_tools=[str(t) for t in declared_tools],
            identity_claims=identity_claims,
            identity_roles=identity_roles,
        )
        # COST-TRACK: trace_id is whatever before_agent set for this run (a
        # fresh one-off if it never fired -- e.g. an older langchain
        # missing the before_agent hook, or this middleware invoked some
        # other way); span_id is fresh per turn and left SET (not reset)
        # so the tool_call(s) this turn triggers -- fired as separate,
        # later graph steps, not nested inside this method -- correlate
        # into the SAME span. See the module-level contextvar comments.
        trace_id = _current_trace_id.get() or _new_trace_id()
        span_id = _new_span_id()
        _current_span_id.set(span_id)
        scope = _ModelCallScope(trace_id=trace_id, span_id=span_id, model=model_name)
        cost_context = _cost_tracker.context_for(trace_id=trace_id, scope_id=span_id)
        principal = _effective_principal(self.caller)
        return snapshot, principal, identity_claims, identity_roles, scope, cost_context

    def _post_model_snapshot(
        self, response: ModelResponse, identity_claims: dict[str, str], identity_roles: list[str]
    ) -> Snapshot:
        response_text = " ".join(_message_text(m) for m in response.result if _message_text(m))
        return Snapshot(
            provider="langgraph",
            endpoint="in-process:langgraph:model_call",
            parsed=True,
            response_preview=response_text[:_PREVIEW_CHARS],
            identity_claims=identity_claims,
            identity_roles=identity_roles,
        )

    def _record_usage(self, scope: _ModelCallScope, response: ModelResponse) -> dict[str, int]:
        """Records this call's usage (if any was reported) against
        scope.trace_id/span_id, then returns the cumulative totals
        AFTER that recording -- so the post-stage Cedar decision sees the
        up-to-date picture, not the pre-call snapshot. Mirrors maf.py's
        own COST-TRACK-1 comment at the equivalent point."""
        prompt_tok, completion_tok, total_tok = _usage_tokens(response)
        if total_tok:
            cost_micros = estimate_cost_usd_micros(scope.model, prompt_tok, completion_tok) or 0
            _cost_tracker.record(
                trace_id=scope.trace_id,
                scope_id=scope.span_id,
                tokens=total_tok,
                cost_usd_micros=cost_micros,
            )
        return _cost_tracker.context_for(trace_id=scope.trace_id, scope_id=scope.span_id)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        pre_snapshot, principal, identity_claims, identity_roles, scope, cost_context = (
            self._pre_model_snapshot(request)
        )
        pre = self.hook.evaluate(
            snapshot=pre_snapshot, stage="pre", principal=principal, extra_context=cost_context
        )
        if not pre.decision.allowed:
            raise GovernanceDenied(pre.decision)

        response = handler(request)

        post_context = self._record_usage(scope, response)
        post_snapshot = self._post_model_snapshot(response, identity_claims, identity_roles)
        post = self.hook.evaluate(
            snapshot=post_snapshot, stage="post", principal=principal, extra_context=post_context
        )
        if not post.decision.allowed:
            raise GovernanceDenied(post.decision)
        # post.alter_with: ALTER is not yet supported by this adapter (see
        # module docstring) -- a bundle that annotates a permit with
        # @action("alter") on a stage this module governs would silently
        # be treated as a plain allow here. Tracked as a known gap, not a
        # silent behavior difference callers should discover by accident.
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        pre_snapshot, principal, identity_claims, identity_roles, scope, cost_context = (
            self._pre_model_snapshot(request)
        )
        pre = self.hook.evaluate(
            snapshot=pre_snapshot, stage="pre", principal=principal, extra_context=cost_context
        )
        if not pre.decision.allowed:
            raise GovernanceDenied(pre.decision)

        response = await handler(request)

        post_context = self._record_usage(scope, response)
        post_snapshot = self._post_model_snapshot(response, identity_claims, identity_roles)
        post = self.hook.evaluate(
            snapshot=post_snapshot, stage="post", principal=principal, extra_context=post_context
        )
        if not post.decision.allowed:
            raise GovernanceDenied(post.decision)
        return response

    # ------------------------------------------------------------------ #
    # tool call -- one Cedar tool_call decision before the tool runs
    # ------------------------------------------------------------------ #

    def _tool_snapshot(self, request: ToolCallRequest) -> tuple[Snapshot, str, dict[str, int]]:
        tool_call = request.tool_call
        tool_args = dict(tool_call.get("args") or {})
        # request.tool -- the resolved BaseTool, with a native `.metadata`
        # dict and (for a StructuredTool) `.func` back to the underlying
        # callable -- used to be read nowhere in this method (a real,
        # previously-shipped gap: auth-integrations.md finding #3). `tool`
        # is None for a tool not registered with the ToolNode; fall back to
        # the raw tool_call dict alone in that case, same as before this
        # fix existed.
        tool = request.tool
        vendor = _resolve_vendor_call_from_metadata(
            getattr(tool, "metadata", None)
        ) or _resolve_vendor_call(getattr(tool, "func", None), tool_args)
        snapshot = Snapshot(
            provider="langgraph",
            endpoint="in-process:langgraph:tool_call",
            parsed=True,
            tool_name=str(tool_call.get("name", "")),
            tool_args=tool_args,
            identity_claims=_effective_identity_claims(None),
            identity_roles=_effective_identity_roles(None),
            vendor_system=vendor[0] if vendor else None,
            vendor_operation=vendor[1] if vendor else None,
            crud_action=vendor[2] if vendor else None,
        )
        # COST-TRACK: scope_id is the TRIGGERING model_call's own span id
        # (whatever wrap_model_call last set -- see the module-level
        # contextvar comments), so this tool_call accumulates into the
        # SAME "turn" total as the model_call that requested it. Falls
        # back to a fresh one-off id only for a tool_call with no
        # correlated model_call at all (this middleware invoked directly,
        # or wrap_model_call never fired -- e.g. a resumed/checkpointed
        # run), same fallback shape as maf.py's own tool_call correlation.
        trace_id = _current_trace_id.get() or _new_trace_id()
        span_id = _current_span_id.get() or _new_span_id()
        cost_context = _cost_tracker.context_for(trace_id=trace_id, scope_id=span_id)
        return snapshot, _effective_principal(self.caller), cost_context

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        snapshot, principal, cost_context = self._tool_snapshot(request)
        decision = self.hook.evaluate(
            snapshot=snapshot, stage="pre", principal=principal, extra_context=cost_context
        )
        if not decision.decision.allowed:
            _record_tool_denial(decision.decision.reason)
            raise GovernanceDenied(decision.decision)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        snapshot, principal, cost_context = self._tool_snapshot(request)
        decision = self.hook.evaluate(
            snapshot=snapshot, stage="pre", principal=principal, extra_context=cost_context
        )
        if not decision.decision.allowed:
            _record_tool_denial(decision.decision.reason)
            raise GovernanceDenied(decision.decision)
        return await handler(request)


@dataclass(slots=True)
class _MiddlewareRegistryEntry:
    engine: PolicyEngine
    middleware: ParapetAgentMiddleware
    stop_event: threading.Event | None
    thread: threading.Thread | None = None


_middleware_registry: dict[tuple[str, str, str, str, str], _MiddlewareRegistryEntry] = {}
_middleware_registry_lock = threading.Lock()


def reset_middleware_registry() -> None:
    """Test-only: stops every background sync thread build_middleware() has
    started in this process and forgets all cached middleware. Mirrors
    parapetai_agent.maf.reset_middleware_registry() exactly -- same reason
    (a poller mid-cycle finishes its current poll_once + heartbeat AFTER a
    bare stop_event.set(), so this also JOINs each poller thread, bounded)."""
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
    console: bool | None = None,
    vendor_scoped_resources: bool = False,
) -> ParapetAgentMiddleware:
    """One PolicyEngine, one Caller, one ParapetAgentMiddleware -- the
    LangGraph/LangChain equivalent of `parapetai_agent.maf.build_middleware()`/
    `parapetai_agent.adk.build_plugin()`, same kwarg surface, same
    semantics for every parameter (policy resolution, control-plane pull,
    Ed25519 PEP identity, OTel auto-wiring, idempotent per-identity
    caching) -- see `maf.build_middleware()`'s own docstring for the full
    story on each; not repeated here since none of it is LangGraph-specific
    (`governance_runtime`/`control_plane`/`pep_identity` are already
    framework-agnostic).

    Register the returned middleware directly:

        from langchain.agents import create_agent
        from parapetai_agent.langgraph import build_middleware

        mw = build_middleware(policy_dir="./policies")
        agent = create_agent(model, tools=tools, middleware=[mw])

    No `alter_transforms=` parameter -- ALTER decisions are not yet
    supported by this adapter (see module docstring's Known Gaps).

    vendor_scoped_resources: see maf.build_middleware()'s own docstring --
    same opt-in, off-by-default flag, same reason (auth-integrations.md
    §3/§8 Q2)."""
    console, local_log_dir = _resolve_local_output_settings(console, local_log_dir)
    if local_log_dir is not None:
        configure_rotating_audit_log(local_log_dir, console=console)

    resolved_control_plane_url = control_plane_url or os.environ.get("PARAPETAI_CONTROL_PLANE_URL")
    resolved_agent_secret = agent_secret or os.environ.get("PARAPETAI_AGENT_SECRET")
    control_plane_configured = bool(resolved_control_plane_url and resolved_agent_secret)

    if control_plane_configured and not otel_configured():
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
            return cached.middleware

        resolved_pep_key_path = (
            (Path(pep_key_path) if pep_key_path else pep_identity.default_key_path())
            if persist_pep_key
            else None
        )

        stop_event: threading.Event | None = None
        poll_thread: threading.Thread | None = None
        if control_plane_configured:
            assert resolved_control_plane_url and resolved_agent_secret  # narrows for mypy
            boot = bootstrap_engine(
                resolved_control_plane_url,
                resolved_agent_secret,
                policy_dir=resolved_policy_dir,
                entities_path=resolved_entities_path,
                persist_policy_dir=persist_policy_dir,
                pep_key_path=resolved_pep_key_path,
                mode="enforce",
                version=_installed_version(),
                poller_name=f"bundle-poll-{resolved_agent_id}",
            )
            engine = boot.engine
            stop_event = boot.stop_event
            poll_thread = boot.thread
            # See maf.build_middleware()'s own comment at this same point:
            # control-plane-resolved (once, at bootstrap) takes priority
            # over the caller's own kwarg once a control plane is
            # configured at all.
            resolved_vendor_scoped_resources = boot.vendor_scoped_resources
        else:
            engine = PolicyEngine(resolved_policy_dir, resolved_entities_path)
            resolved_vendor_scoped_resources = vendor_scoped_resources

        caller = Caller(agent_id=resolved_agent_id, tenant=tenant)
        middleware = ParapetAgentMiddleware(
            engine, caller, vendor_scoped_resources=resolved_vendor_scoped_resources
        )
        _middleware_registry[key] = _MiddlewareRegistryEntry(
            engine, middleware, stop_event, poll_thread
        )
        return middleware
