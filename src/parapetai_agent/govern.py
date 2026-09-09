"""Framework-neutral governance — govern any agent loop, no framework required.

`parapetai_agent.maf` wires governance into the Microsoft Agent Framework's
middleware. This module does the same job with **no framework at all**: three
explicit calls any agent loop, tool wrapper, or framework callback can make.

    from parapetai_agent import Governor, GovernanceDenied

    gov = Governor.from_policy_dir("./policies")

    gov.check_input(prompt, roles=["OrderViewer"])   # before the model
    gov.authorize_tool("delete_incident", {...})     # before a tool runs -> may raise
    gov.check_output(answer, sources=[doc])          # after the model

Under the hood this is the SAME decision path `maf.py` uses — a
`GovernanceHook` over a `PolicyEngine` — so a decision made here is identical
to one made through the MAF adapter. Adding governance to LangGraph, CrewAI,
the OpenAI Agents SDK, or a plain `while` loop is just calling these methods at
that framework's own tool/model hook points.

Every method returns the Cedar `Decision`. By default a deny raises
`GovernanceDenied` (so a denied tool call never reaches your tool); pass
`raise_on_deny=False` to get the `Decision` back and branch on it yourself.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import context as _otel_context_api
from opentelemetry import trace as _otel_trace

if TYPE_CHECKING:
    from parapetai_agent.control_plane import Bootstrap, ReviewClient

from parapetai_agent import observation as _observation
from parapetai_agent._exceptions import GovernanceDenied, GovernanceReviewRequired
from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.governance_runtime import (
    parent_context_from_span_context as _parent_context_from_span_context,
)
from parapetai_agent.groundedness import GroundednessConfig
from parapetai_agent.identity import Caller
from parapetai_agent.policy.cost_tracker import CostTracker
from parapetai_agent.policy.cost_tracker import new_span_id as _new_span_id
from parapetai_agent.policy.cost_tracker import new_trace_id as _new_trace_id
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook, OnDecision
from parapetai_agent.policy.pricing import estimate_cost_usd_micros
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.response_judge import JudgeConfig
from parapetai_agent.vendor_calls import resolve_vendor_call as _resolve_vendor_call
from parapetai_agent.vendor_calls import (
    resolve_vendor_call_from_metadata as _resolve_vendor_call_from_metadata,
)

__all__ = ["Governor", "GovernanceDenied", "GovernanceReviewRequired"]

# Chars of prompt/response kept SDK-side so scanners can see them. Never logged:
# the audit record is content-free (parapetai_agent.policy.hooks.content_free).
_PREVIEW = 4000
_PROVIDER = "govern"

# One CostTracker per process -- same module-level-singleton pattern as
# maf.py/adk.py/langgraph.py's own _cost_tracker; trace_id is globally
# unique (random hex), so sharing across every Governor instance in this
# process is safe. See docs/reference/cost-tracking.md.
_cost_tracker = CostTracker()

# TRACE/SPAN correlation for cost tracking -- see Governor.trace()'s own
# docstring for why this has to be explicit here (no framework loop of its
# own to hook a "run started"/"run ended" boundary into, unlike MAF/ADK/
# LangGraph). contextvars for the same reason maf.py's _current_chat is
# one: isolated per asyncio task under concurrent callers sharing one
# Governor. _current_span_id is set by check_input() and deliberately LEFT
# SET (not reset in a finally) so a later authorize_tool()/check_output()
# call in the same turn -- called separately, by the embedding loop, not
# nested inside check_input() -- picks up the same scope.
_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_govern_current_trace_id", default=None
)
_current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_govern_current_span_id", default=None
)

# auth-integrations.md §10.7: a real OTel span for check_input()/
# authorize_tool(), same lazy-proxy pattern maf.py's own module-level
# _tracer already is -- see that module's comment on configure_otel()
# ordering.
_tracer = _otel_trace.get_tracer(__name__)


class _OpenSpan:
    """One real OTel span this class opened on a caller's behalf, made
    AMBIENT (via opentelemetry.context.attach(), not a `with` block) for
    whatever the CALLER runs next -- Governor has no framework loop and no
    "wrap this call" API the way MAF/ADK/LangGraph's middleware do
    (check_input()/authorize_tool() are standalone calls; the real network
    call a tool makes happens in the embedding application's OWN code,
    immediately after authorize_tool() returns, not inside anything this
    class controls). attach() is the low-level OTel mechanism for "make
    this the current span starting now, until something detaches it" --
    the same effect a `with start_as_current_span():` block gives other
    adapters, without requiring one call frame to bracket both open and
    close.

    Lifecycle (there is no natural "after tool"/"after model call" hook to
    close this deterministically against, so it closes on whichever comes
    first):
      - superseded: the NEXT check_input()/authorize_tool() call in the
        same trace supersedes the previous one of its own kind -- see
        _supersede() below, mirroring _current_span_id's own existing
        "left set until superseded" behavior for the string ids.
      - trace() exit: Governor.trace()'s `finally` block closes out
        whatever is still open, so nothing leaks past the trace boundary
        even if the caller never made a following call.

    A real risk this accepts, matching the SAME risk _current_span_id's
    own "falls back to a fresh one-off id" comment already documents for
    cross-call correlation generally: if the embedding application hands
    the actual tool/model call off to a DIFFERENT thread or a detached
    asyncio Task before making it, this attached context (like any
    contextvar-based one) will not be visible there -- out of this
    class's control either way.

    close() is defensive about a real failure mode confirmed live while
    building this: `opentelemetry.context.detach()` requires the SAME
    `contextvars.Context` the corresponding attach() ran in -- calling
    authorize_tool() through an async wrapper (`Governor.tool`'s own
    `@gov.tool` decorator) across separate `await` invocations can
    genuinely land close() in a DIFFERENT context than open() ran in,
    which raises `ValueError: ... was created in a different Context`.
    Uncaught, that turns a detection feature into a crash in the
    CALLER's own tool-call path -- strictly worse than a slightly-stale
    attachment. `framework_scope.reset()` is separately defensive against
    this exact failure mode already (see FrameworkScope.reset()'s own
    docstring in observation.py) -- only the lower-level
    opentelemetry.context.detach() call below needs its own guard here."""

    __slots__ = ("span", "_context_token", "framework_scope")

    def __init__(self, span: _otel_trace.Span, framework_scope: _observation.FrameworkScope):
        self.span = span
        self.framework_scope = framework_scope
        self._context_token = _otel_context_api.attach(_otel_trace.set_span_in_context(span))

    def close(self) -> None:
        try:
            _otel_context_api.detach(self._context_token)
        except ValueError:
            pass
        self.framework_scope.reset()
        self.span.end()


_current_model_span: contextvars.ContextVar[_OpenSpan | None] = contextvars.ContextVar(
    "parapetai_agent_govern_current_model_span", default=None
)
_current_tool_span: contextvars.ContextVar[_OpenSpan | None] = contextvars.ContextVar(
    "parapetai_agent_govern_current_tool_span", default=None
)


def _supersede(var: contextvars.ContextVar[_OpenSpan | None], new: _OpenSpan) -> None:
    """Closes whatever _OpenSpan `var` currently holds (if any) before
    installing `new` -- the span-lifecycle equivalent of _current_span_id's
    plain `.set()` overwrite, made explicit here because an OTel span (
    unlike a bare string id) needs an actual close() call to stop leaking
    its context attachment and to actually get exported."""
    previous = var.get()
    if previous is not None:
        previous.close()
    var.set(new)


class Governor:
    """A framework-neutral governance entry point over one policy set.

    Construct it once (from a local policy dir, or an in-memory bundle), then
    call `check_input` / `authorize_tool` / `check_output` from wherever your
    agent framework fires. Identity is passed per call (`roles=`, `claims=`);
    with none supplied the caller is unauthenticated, which Cedar evaluates
    under its default-deny policy set — never a bypass.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        caller: Caller | None = None,
        content_checks: ContentCheckConfig | None = None,
        groundedness: GroundednessConfig | None = None,
        judge: JudgeConfig | None = None,
        on_decision: OnDecision | None = None,
        vendor_scoped_resources: bool = False,
    ) -> None:
        self._engine = engine
        self._caller = caller or Caller(agent_id="agent")
        self._hook = GovernanceHook(
            engine,
            self._caller,
            on_decision=on_decision,
            vendor_scoped_resources=vendor_scoped_resources,
        )
        self._content_checks = content_checks
        self._groundedness = groundedness
        self._judge = judge
        # Set only by from_control_plane(), which owns a poller thread. None
        # for every locally-constructed Governor, so stop_sync() is safe to
        # call regardless of how this was built.
        self._bootstrap: Bootstrap | None = None
        # Likewise None for a locally-constructed Governor: with no control
        # plane there is no queue and therefore no human to ask, so a review
        # stays a plain deny. Approvals are an affordance a connected PEP
        # gains, never a requirement local policy enforcement takes on.
        self._reviews: ReviewClient | None = None

    # ------------------------------------------------------------------ #
    # constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def from_policy_dir(
        cls,
        policy_dir: str | Path,
        entities_path: str | Path | None = None,
        *,
        bundle_files: Mapping[str, str] | None = None,
        caller: Caller | None = None,
        on_decision: OnDecision | None = None,
        vendor_scoped_resources: bool = False,
    ) -> Governor:
        """Load Cedar policy from local files. `bundle_files` optionally supplies
        the content-check / groundedness / judge JSON configs (the same files a
        control-plane bundle carries) to enable the input scanners and output
        evals; without them, only Cedar authorization runs.

        vendor_scoped_resources (default False): same opt-in, off-by-default
        flag as maf.build_middleware()/adk.build_plugin()/langgraph.build_middleware()
        -- see docs/reference/vendor-calls.md. Meaningful here unconditionally
        (unlike those three), since from_control_plane() below resolves it
        from the bundle instead and never reads this parameter at all."""
        engine = PolicyEngine(policy_dir, entities_path)
        cc, gr, jd = ContentCheckConfig(), GroundednessConfig(), JudgeConfig()
        if bundle_files:
            files = dict(bundle_files)
            cc.load_from_bundle(files)
            gr.load_from_bundle(files)
            jd.load_from_bundle(files)
        return cls(
            engine,
            caller=caller,
            content_checks=cc,
            groundedness=gr,
            judge=jd,
            on_decision=on_decision,
            vendor_scoped_resources=vendor_scoped_resources,
        )

    @classmethod
    def from_control_plane(
        cls,
        control_plane_url: str | None = None,
        agent_secret: str | None = None,
        *,
        policy_dir: str | Path,
        entities_path: str | Path | None = None,
        persist_policy_dir: str | Path | None = None,
        pep_key_path: str | Path | None = None,
        agent_id: str | None = None,
        tenant: str = "default",
        mode: str = "enforce",
        caller: Caller | None = None,
        on_decision: OnDecision | None = None,
    ) -> Governor:
        """Govern from CONTROL-PLANE-authored policy, refreshed in the
        background -- the framework-neutral equivalent of what
        `parapetai_agent.maf.build_middleware(control_plane_url=..., agent_secret=...)`
        does for MAF.

        Without this, the only way to get control-plane policy into an
        embedded agent was the MAF adapter, and every other framework
        (LangGraph, CrewAI, the OpenAI Agents SDK, a plain loop) was stuck on
        `from_policy_dir()` -- policy files the adopter maintains themselves,
        which is not governed by the control plane at all. That gap made the
        product promise ("policy is defined in the control plane; the agent
        syncs so it acts as configured") true for exactly one framework.

        Fetches the signed bundle, applies it, and starts the background
        poller so later edits and approvals land without a restart. Every
        decision is still evaluated LOCALLY, in-process -- the control plane
        is never on the decision path, so it can be down without blocking a
        single call.

        ON AN UNREACHABLE CONTROL PLANE it degrades to the last bundle on
        disk rather than refusing to start (see
        control_plane.bootstrap_engine for the exact semantics) -- an outage
        on our side must not take a customer's agent down. With `policy_dir`
        empty and nothing yet persisted there is no policy to enforce at all,
        and PolicyEngine's constructor raises: fail closed.

        `control_plane_url`/`agent_secret` fall back to
        PARAPETAI_CONTROL_PLANE_URL / PARAPETAI_AGENT_SECRET, matching
        build_middleware, so the same env that configures a MAF agent
        configures this one.

        The returned Governor owns a daemon poller thread; call
        `.stop_sync()` to end it (tests, or a process that constructs many).
        """
        from parapetai_agent.control_plane import (
            ReviewClient,
            bootstrap_engine,
            default_pep_id,
            sdk_version,
        )

        url = control_plane_url or os.environ.get("PARAPETAI_CONTROL_PLANE_URL")
        secret = agent_secret or os.environ.get("PARAPETAI_AGENT_SECRET")
        if not url or not secret:
            raise RuntimeError(
                "Governor.from_control_plane needs a control plane URL and agent secret "
                "(arguments, or PARAPETAI_CONTROL_PLANE_URL / PARAPETAI_AGENT_SECRET). "
                "Use Governor.from_policy_dir() for local or air-gapped policy."
            )

        # Constructed unconditionally, then populated from every fetched
        # bundle -- same contract as build_middleware: an SDK new enough to
        # have these modules enforces whatever config its bundle carries,
        # with no extra flag for an adopter to remember to set.
        cc, gr, jd = ContentCheckConfig(), GroundednessConfig(), JudgeConfig()

        def _load_bundle_configs(files: dict[str, str]) -> None:
            # All three refresh from the SAME bundle on every poll, so the
            # input scanners and output evals stay in lockstep with policy.
            cc.load_from_bundle(files)
            gr.load_from_bundle(files)
            jd.load_from_bundle(files)

        resolved_agent_id = agent_id or os.environ.get("PARAPETAI_AGENT_ID") or "agent"
        boot = bootstrap_engine(
            url,
            secret,
            policy_dir=policy_dir,
            entities_path=entities_path,
            persist_policy_dir=persist_policy_dir,
            pep_key_path=pep_key_path,
            mode=mode,
            # Same value the MAF adapter sends, so the fleet table reports a
            # PEP's SDK build identically however the customer embedded it.
            version=sdk_version(),
            poller_name=f"bundle-poll-{resolved_agent_id}",
            on_bundle=_load_bundle_configs,
        )
        governor = cls(
            boot.engine,
            caller=caller or Caller(agent_id=resolved_agent_id, tenant=tenant),
            content_checks=cc,
            groundedness=gr,
            judge=jd,
            on_decision=on_decision,
            # Control-plane-resolved, once at bootstrap -- same priority
            # rule as build_middleware()/build_plugin(): a tenant's
            # console-driven rollout decision is authoritative, so there is
            # no separate vendor_scoped_resources= parameter on THIS
            # constructor for a caller to override it with (unlike
            # from_policy_dir(), which has no bundle to resolve one from at
            # all). See Bootstrap.vendor_scoped_resources's own docstring
            # for why this doesn't hot-reload mid-process.
            vendor_scoped_resources=boot.vendor_scoped_resources,
        )
        governor._bootstrap = boot
        governor._reviews = ReviewClient(
            control_plane_url=url,
            agent_secret=secret,
            agent_id=resolved_agent_id,
            private_key=boot.private_key,
            pep_id=default_pep_id(),
        )
        return governor

    def stop_sync(self, timeout: float | None = None) -> None:
        """Stop the background bundle poller, if this Governor started one.
        A no-op for a Governor built from local policy -- so a caller can
        always call it without knowing which constructor was used."""
        if self._bootstrap is not None:
            self._bootstrap.stop(timeout)
            self._bootstrap = None

    # ------------------------------------------------------------------ #
    # cumulative cost/token tracking scope (see docs/reference/cost-tracking.md)
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def trace(self) -> Iterator[None]:
        """Marks one cumulative cost/token-tracking TRACE boundary --
        every check_input()/authorize_tool()/check_output() call made
        inside this block shares one running total
        (`context.trace_cumulative_tokens` etc.), dropped the moment the
        block exits:

            with gov.trace():
                gov.check_input(prompt)
                gov.authorize_tool("lookup_order", {"id": "123"})
                gov.check_output(answer, model="gpt-4o", completion_tokens=42)

        MAF/ADK/LangGraph each have a real "one agent run" boundary their
        own middleware sees (`before_agent`/`after_agent`, an OTel span, or
        equivalent) and use it to scope this automatically. `Governor` has
        no framework loop of its own to hook into (see
        docs/frameworks/governor.md's Async/streaming section) -- so unlike
        those three, this has to be explicit.

        Without it, every call is its own one-off trace: cumulative fields
        are still always populated (never absent from `context`), they
        just never accumulate past that single call -- the same degrade
        LangGraph's own adapter falls back to when its `before_agent` hook
        never fired. Nestable (an inner `with gov.trace():` gets its own
        trace_id and the outer one resumes on exit), but nesting does NOT
        merge totals across the two -- they are two independent traces.
        """
        token = _current_trace_id.set(_new_trace_id())
        try:
            yield
        finally:
            trace_id = _current_trace_id.get()
            if trace_id is not None:
                _cost_tracker.end_trace(trace_id)
            _current_trace_id.reset(token)
            # auth-integrations.md §10.7: neither check_input()'s model_call
            # span nor authorize_tool()'s tool_call span has a natural
            # "after" call to close against (see _OpenSpan's own docstring)
            # -- the trace boundary is the backstop that guarantees neither
            # ever leaks past it unclosed, even if the caller's last turn
            # never made a following call that would have superseded it.
            open_tool = _current_tool_span.get()
            if open_tool is not None:
                open_tool.close()
                _current_tool_span.set(None)
            open_model = _current_model_span.get()
            if open_model is not None:
                open_model.close()
                _current_model_span.set(None)

    # ------------------------------------------------------------------ #
    # the three decisions
    # ------------------------------------------------------------------ #
    def check_input(
        self,
        text: str,
        *,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        model: str | None = None,
        tools: Sequence[str] | None = None,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Pre-model guardrail: run any configured input scanners (PII, secrets,
        injection) and a Cedar `model_call` decision before the model sees the
        prompt."""
        claims_d, roles_l = self._identity(claims, roles)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:model_call",
            parsed=True,
            model=model,
            messages_preview=str(text)[:_PREVIEW],
            declared_tools=list(tools or []),
            identity_claims=claims_d,
            identity_roles=roles_l,
        )
        extra: dict[str, Any] = {}
        if self._content_checks is not None:
            # evaluate() is a harmless no-op when no scanners are configured.
            res = self._content_checks.evaluate(snap)
            if res.errors:  # a configured scanner could not run -> fail closed
                return self._deny(self._failure_decision(res.errors), raise_on_deny)
            extra = res.context
        # COST-TRACK: trace_id is whatever trace() set (a fresh one-off if
        # that context manager was never entered -- degrades to per-call
        # granularity, never absent). span_id is fresh per check_input()
        # call and left SET (not reset) so authorize_tool()/check_output()
        # calls the embedding loop makes AFTER this returns -- not nested
        # inside it -- pick up the same turn. Mirrors langgraph.py's
        # wrap_model_call/_current_span_id exactly.
        trace_id = _current_trace_id.get() or _new_trace_id()
        span_id = _new_span_id()
        _current_span_id.set(span_id)
        extra = {**extra, **_cost_tracker.context_for(trace_id=trace_id, scope_id=span_id)}
        # auth-integrations.md §10.7: a real OTel span, ambient for
        # whatever the caller does next (see _OpenSpan's own docstring for
        # why this can't be a `with` block here) -- superseded, not
        # stacked, if a PREVIOUS check_input() in this trace never got
        # superseded by its own next call (mirrors _current_span_id's
        # existing plain-overwrite behavior above, made explicit because a
        # real span needs closing, not just overwriting).
        model_span = _tracer.start_span("parapetai.model_call")
        model_scope = _observation.set_current_framework("governor")
        _supersede(_current_model_span, _OpenSpan(model_span, model_scope))
        result = self._hook.evaluate(snapshot=snap, stage="pre", extra_context=extra or None)
        if not result.decision.allowed:
            model_span.set_status(
                _otel_trace.Status(_otel_trace.StatusCode.ERROR, result.decision.reason)
            )
        # No args_preview: the "arguments" of a model call are the prompt, and
        # invariant 10 keeps prompt content out of anything the control plane
        # stores unless someone explicitly opts in. The fingerprint still binds
        # the grant to this exact prompt -- a digest is not content.
        return self._finish(
            result.decision, raise_on_deny, action="model_call", args={"text": str(text)}
        )

    def authorize_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        func: Callable[..., Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Authorize one tool call — by name, arguments, and caller role —
        against Cedar, before it executes. A denied call raises (default) so it
        never runs.

        `func`/`metadata` declare the tool's vendor/CRUD facts, same two
        paths and same precedence (metadata checked first) `adk.py`/
        `langgraph.py` use — see docs/reference/vendor-calls.md:

        - `func`: the underlying callable, if it (or a wrapper `.tool()`
          resolved to it) was decorated with
          `parapetai_agent.vendor_calls.declare_vendor_call`. `Governor.tool`
          passes this for you automatically.
        - `metadata`: a plain dict for a tool you don't own the source of
          (the same `parapet_vendor_system`/`parapet_resource_type`/
          `parapet_crud_action` convention ADK's `custom_metadata`/
          LangChain's `.metadata` use) — the natural path here, since a raw
          `authorize_tool()` call has no framework tool object to read a
          native metadata dict off of at all.

        Populating either reaches the SAME `context.vendor_system`/
        `crud_action` fields, and is subject to the SAME opt-in
        `vendor_scoped_resources` resource construction, that MAF/ADK/
        LangGraph already produce — a control-plane connector-catalog
        match (or a hand-written Cedar policy) that reads those fields
        needs no special case for "this call came through Governor."
        """
        claims_d, roles_l = self._identity(claims, roles)
        args = dict(arguments or {})
        vendor = _resolve_vendor_call_from_metadata(metadata) or _resolve_vendor_call(func, args)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:tool_call",
            parsed=True,
            tool_name=name,
            tool_args=args,
            identity_claims=claims_d,
            identity_roles=roles_l,
            vendor_system=vendor[0] if vendor else None,
            vendor_operation=vendor[1] if vendor else None,
            crud_action=vendor[2] if vendor else None,
            framework="governor",
        )
        # COST-TRACK: scope_id is whatever check_input() last set (the
        # triggering model_call's own "turn"), so this tool_call
        # accumulates into the SAME total as the model_call that requested
        # it -- falls back to a fresh one-off id only when authorize_tool()
        # is called with no preceding check_input() in this trace at all
        # (a valid, common pattern -- e.g. a caller that only ever governs
        # tool calls, per this module's own top-of-file example).
        trace_id = _current_trace_id.get() or _new_trace_id()
        span_id = _current_span_id.get() or _new_span_id()
        cost_context = _cost_tracker.context_for(trace_id=trace_id, scope_id=span_id)
        # auth-integrations.md §10.7: a real OTel span, explicitly parented
        # to whatever check_input() last opened (if anything -- a Governor
        # used tool-call-only, per this module's own top-of-file example,
        # has no model_call span to parent to, and gets a root span
        # instead, same as an unresolved parent anywhere else in this
        # codebase). Made ambient via attach(), not a `with` block -- see
        # _OpenSpan's own docstring for why: the real network call this
        # tool makes happens in the CALLER's own code, immediately after
        # this method returns, not inside anything authorize_tool()
        # controls.
        preceding_model = _current_model_span.get()
        tool_span = _tracer.start_span(
            "parapetai.tool_call",
            context=_parent_context_from_span_context(
                preceding_model.span.get_span_context() if preceding_model else None
            ),
        )
        tool_scope = _observation.set_current_framework("governor")
        _supersede(_current_tool_span, _OpenSpan(tool_span, tool_scope))
        result = self._hook.evaluate(snapshot=snap, stage="pre", extra_context=cost_context)
        if not result.decision.allowed:
            tool_span.set_status(
                _otel_trace.Status(_otel_trace.StatusCode.ERROR, result.decision.reason)
            )
        # Tool arguments ARE previewable: they are what the policy already
        # matched on, and an approver who cannot see which issue is being
        # closed cannot meaningfully approve closing it.
        return self._finish(
            result.decision,
            raise_on_deny,
            action="tool_call",
            tool_name=name,
            args=args,
            preview=json.dumps(args, sort_keys=True, default=str)[:2000],
        )

    def check_output(
        self,
        response: str,
        *,
        sources: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Post-model eval: score groundedness (against `sources`) and run the
        SLM judge if configured, then a Cedar `post` decision — before the
        answer is delivered. A scorer that errors fails closed (denies).

        `prompt_tokens`/`completion_tokens` (both default 0, meaning "no
        usage to report") let the caller feed real usage back into
        cumulative cost/token tracking — unlike MAF/ADK, `Governor` never
        sees the model's own response object (only this already-extracted
        `text`), so it cannot learn token counts on its own; the embedding
        loop, which DOES hold the real response, has to report them. Cost
        is estimated the same way MAF/ADK's is, via the SAME
        `$/1M token` price table (`policy/pricing.py`,
        `PARAPETAI_MODEL_PRICING`) — the caller reports tokens, not
        dollars. See docs/reference/cost-tracking.md."""
        claims_d, roles_l = self._identity(claims, roles)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:model_call",
            parsed=True,
            model=model,
            response_preview=str(response)[:_PREVIEW],
            identity_claims=claims_d,
            identity_roles=roles_l,
        )
        extra: dict[str, Any] = {}
        errors: list[str] = []
        source_text = " ".join(s for s in sources if s) if sources else ""
        if self._groundedness is not None and self._groundedness.active and source_text:
            g = self._groundedness.evaluate_post(str(response), source_text)
            errors.extend(g.errors)
            extra.update(g.context)
        if self._judge is not None and self._judge.active:
            j = self._judge.evaluate_post(str(response))
            errors.extend(j.errors)
            extra.update(j.context)
        if errors:  # a scorer could not run -> fail closed
            return self._deny(self._failure_decision(tuple(errors)), raise_on_deny)
        # COST-TRACK: scope_id is whatever check_input() last set for this
        # turn (a fresh one-off if check_output() is called standalone).
        # Recorded BEFORE building extra, same as maf.py's own COST-TRACK-1
        # ordering, so the post-stage Cedar decision sees the up-to-date
        # picture including this call's own usage, not the pre-call one.
        trace_id = _current_trace_id.get() or _new_trace_id()
        span_id = _current_span_id.get() or _new_span_id()
        if prompt_tokens or completion_tokens:
            total_tok = prompt_tokens + completion_tokens
            cost_micros = estimate_cost_usd_micros(model, prompt_tokens, completion_tokens) or 0
            _cost_tracker.record(
                trace_id=trace_id, scope_id=span_id, tokens=total_tok, cost_usd_micros=cost_micros
            )
        extra.update(_cost_tracker.context_for(trace_id=trace_id, scope_id=span_id))
        result = self._hook.evaluate(snapshot=snap, stage="post", extra_context=extra or None)
        # Same content rule as check_input: the response is model output, so it
        # is fingerprinted but never previewed into the queue.
        return self._finish(
            result.decision, raise_on_deny, action="model_response", args={"text": str(response)}
        )

    # ------------------------------------------------------------------ #
    # convenience: a decorator that authorizes a tool before it runs
    # ------------------------------------------------------------------ #
    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
    ) -> Callable[..., Any]:
        """Wrap a tool function so it is authorized (by name + keyword args)
        before it runs; a denial raises `GovernanceDenied` and the body never
        executes. Works on sync and async functions.

            @gov.tool
            def delete_incident(number: str) -> str: ...

        If `f` also carries `@declare_vendor_call` (from
        `parapetai_agent.vendor_calls`), that's resolved automatically and
        reaches Cedar as `context.vendor_system`/`crud_action`, same as it
        would through MAF/ADK/LangGraph — see `authorize_tool()`'s own
        docstring:

            @gov.tool
            @declare_vendor_call(VendorCallSpec(
                vendor_system="salesforce", resource_type="Case", crud_action="delete",
            ))
            def delete_salesforce_case(case_id: str) -> str: ...
        """

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = str(name or getattr(f, "__name__", "tool"))
            if inspect.iscoroutinefunction(f):

                @functools.wraps(f)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    self.authorize_tool(tool_name, kwargs, func=f)
                    return await f(*args, **kwargs)

                return awrapper

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.authorize_tool(tool_name, kwargs, func=f)
                return f(*args, **kwargs)

            return wrapper

        return deco(fn) if fn is not None else deco

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _identity(
        claims: Mapping[str, Any] | None, roles: Sequence[str] | None
    ) -> tuple[dict[str, str], list[str]]:
        if claims is None and roles is None:
            return {}, []  # unauthenticated: Cedar sees no identity_roles
        return (
            {str(k): str(v) for k, v in (claims or {}).items()},
            [str(r) for r in (roles or [])],
        )

    def _failure_decision(self, errors: tuple[str, ...]) -> Decision:
        try:
            gen = int(self._engine.status.get("generation", 0))
        except Exception:  # noqa: BLE001 -- generation is audit-only here
            gen = 0
        reason = f"content check scanner failure: {'; '.join(errors)}"
        return Decision(False, "deny", reason, gen, 0.0, errors=tuple(errors))

    def _finish(
        self,
        decision: Decision,
        raise_on_deny: bool,
        *,
        action: str = "",
        tool_name: str | None = None,
        args: Mapping[str, Any] | None = None,
        preview: str | None = None,
    ) -> Decision:
        if decision.requires_review and raise_on_deny:
            # Queued only on the raising path, which is the default and the
            # only one that can hand the caller a review_id -- Decision is
            # frozen, so a non-raising return has nowhere to carry one, and
            # silently queueing a review the caller can never poll would just
            # accumulate unanswerable rows in an operator's queue. A
            # raise_on_deny=False caller asks for it explicitly via
            # request_approval().
            review_id, fingerprint = self.request_approval(
                decision, action=action, tool_name=tool_name, args=args, preview=preview
            )
            raise GovernanceReviewRequired(decision, review_id=review_id, fingerprint=fingerprint)
        if raise_on_deny and not decision.allowed:
            raise GovernanceDenied(decision)
        return decision

    def _deny(self, decision: Decision, raise_on_deny: bool) -> Decision:
        if raise_on_deny:
            raise GovernanceDenied(decision)
        return decision

    # ------------------------------------------------------------------ #
    # approvals (ADR 0009)
    # ------------------------------------------------------------------ #
    def request_approval(
        self,
        decision: Decision,
        *,
        action: str = "",
        tool_name: str | None = None,
        args: Mapping[str, Any] | None = None,
        preview: str | None = None,
    ) -> tuple[str | None, str]:
        """Queue a held call for a human. Returns `(review_id, fingerprint)`.

        `review_id` is None when there is no control plane configured, or it
        could not be reached. Neither is an error to handle: the call was
        already denied locally and stays denied -- there is simply nobody to
        ask. This is what keeps the control plane on the approval path and off
        the decision path.

        Called for you by the default `raise_on_deny=True` path; call it
        directly only if you passed `raise_on_deny=False` and want the review
        anyway.
        """
        fingerprint = ""
        if self._reviews is None:
            return None, fingerprint
        fingerprint = self._reviews.fingerprint(action=action, tool_name=tool_name, args=args)
        body = self._reviews.submit(
            fingerprint=fingerprint,
            tool_name=tool_name,
            action=action,
            # ADR 0008: a review resolves annotations, and they are the only
            # channel by which the policy author's reviewer-facing detail
            # reaches this queue. A hard deny carries none, by design.
            policy_id=decision.determining_policies[0] if decision.determining_policies else None,
            reason=decision.annotations.get("review_reason") or decision.reason,
            risk_score=decision.annotations.get("risk_score"),
            args_preview=preview,
        )
        review_id = body.get("review_id") if body else None
        return (str(review_id) if review_id else None), fingerprint

    def wait_for_approval(
        self,
        held: GovernanceReviewRequired,
        *,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
    ) -> bool:
        """Block until a human answers the held call. True means approved AND
        collected -- the caller may proceed exactly once.

        Takes the raised exception rather than a bare review_id because
        collecting a grant needs the call's fingerprint too, and the exception
        already carries both. Passing them separately would let a caller
        collect one review's grant while about to perform a different call --
        the control plane refuses that, but the API should not invite it.

        Returns False for every other outcome (denied, expired, never queued,
        control plane unreachable, timed out) so a caller has one thing to
        check. False is always safe: it means the local deny stands.

        Polling, not a held connection -- an approval takes as long as a human
        takes, and nothing should keep an HTTP request open for minutes.
        """
        if self._reviews is None or not held.review_id:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            body = self._reviews.collect(
                review_id=held.review_id, fingerprint=held.fingerprint or ""
            )
            if body is not None:
                if body.get("allowed"):
                    return True
                # Terminal states end the wait immediately: nobody is coming to
                # change a denied or expired review, and polling one until the
                # timeout only delays the caller's own error path.
                if body.get("status") in ("denied", "expired", "consumed"):
                    return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
