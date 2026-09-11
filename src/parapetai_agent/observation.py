"""auth-integrations.md §10.2/§10.3/§10.17: automatic VendorScopePermission
detection -- raw observation capture and the server-controlled collection
budget, shared by both halves of the coverage matrix (§10.7): the
in-process SDK's corroboration-piggybacked span tagging (Phase A, below)
and the gateway's direct-construction MCP path (Phase B,
emit_observed_span() below), which parses a call's shape off the wire with
certainty and so has no ambient network-library span to piggyback onto.

WHAT THIS MODULE DOES AND DOES NOT DO. Detection here means CAPTURE only,
never classification -- §10.0's whole point is that classifying a raw
observation into a VendorScopePermission (vendor/product/resource/
permission) is a control-plane job, over already-ingested OTLP data, not
something this process decides. This module's entire job is: (a) tag the
real network child spans corroboration.py's instrumentors already produce
with just enough extra, still-content-free context (§4.1/finding #10's
"field names only, never values" convention) that the control plane CAN
classify them later, and (b) honor a server-issued instruction to stop
doing that for a specific call shape once enough samples exist, per
§10.3.

RELATIONSHIP TO corroboration.py. This module does not instrument
anything itself -- it rides on top of whatever corroboration.py already
turned on (enable_http_corroboration()/enable_db_corroboration()). A span
those instrumentors create is tagged here IF this module's own
ObservationSpanProcessor is also registered (enable_observation_capture());
if it isn't, corroboration's original drift-comparison purpose is
unaffected either way -- the two are independently toggleable, exactly
like enable_http_corroboration()/enable_db_corroboration() already are
from each other.

PROTOCOL DETECTION IS ATTRIBUTE-BASED, NOT SCOPE-NAME-BASED. Rather than
maintaining a second list of "which library maps to which protocol" in
parallel to corroboration.py's own _CANDIDATES/_DB_CANDIDATES, a started
span is classified by which OTel semantic-convention attributes are
already present on it at on_start time (rpc.system -> grpc, db.system ->
db, any http.*/url.* key -> http) -- read defensively across both the
pre-1.20-ish and post-stabilization semconv attribute names, since which
one a given opentelemetry-instrumentation-* version emits has moved
before and will again (same "read external library output defensively"
discipline this codebase already applies to cedarpy responses). This also
means parapetai.tool_call/parapetai.model_call spans are never
misclassified as an observation -- they carry none of these keys -- with
no explicit exclusion list required.

BUCKET KEY IS A CROSS-REPO CONTRACT. bucket_key() below must produce the
BYTE-IDENTICAL string a Phase C control-plane implementation computes for
the same (agent_id, protocol, verb, target) tuple -- the whole saturation
mechanism (§10.3) depends on the SDK being able to recognise its own
bucket in a server-issued saturated-bucket list. Treat a change to this
function's algorithm the same as CLAUDE.md's Architecture table already
treats signing.signing_payload: change it in exactly one place, and only
alongside the control-plane side.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog
from opentelemetry import trace as _otel_trace

log = structlog.get_logger(__name__)

# One tracer for this whole module's own span-creating callers
# (emit_observed_span, and enable_mcp_observation's patched call_tool
# below) -- same per-instrumentation-scope convention every framework
# adapter already uses. Safe at import time regardless of whether
# configure_otel() has run yet: get_tracer() returns a lazy proxy that
# only resolves against whatever TracerProvider is current at the
# MOMENT a span is actually started, not at this assignment.
_tracer = _otel_trace.get_tracer(__name__)

# --------------------------------------------------------------------------
# Framework identity -- so a child network span can be tagged with which
# in-process adapter (maf | adk | langgraph | governor) opened the ambient
# parapetai.tool_call span it's nested under. Set by each adapter around
# its own tool-call span-open (see maf.py/adk.py's set_current_framework()
# call sites); read here, in ObservationSpanProcessor.on_start, from
# whatever context is ambient when the CHILD span starts -- which is
# exactly the adapter's own `with ...` block, since that's what makes it
# ambient in the first place.
# --------------------------------------------------------------------------

_current_framework: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_observation_current_framework", default=None
)


class FrameworkScope:
    """Returned by set_current_framework(); activates IMMEDIATELY on
    construction (the ContextVar is set before __init__ returns), not on
    __enter__ -- deliberately, so the exact same object works both as a
    `with set_current_framework("maf"):` block (MAF/LangGraph/Governor:
    one call wraps the whole tool-call span, `__exit__` calls reset())
    AND as a bare call bracketing two separate callback invocations
    (ADK: before_tool_callback constructs and stores it,
    after_tool_callback retrieves it and calls .reset() -- there is no
    single call frame to wrap in a `with` block, since ADK's own runtime
    invokes the tool itself in between the two callbacks). `__enter__`
    is a no-op re-return of self, not a second `.set()` call, so entering
    an already-active scope via `with` doesn't leak a second token."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._token: contextvars.Token[str | None] | None = _current_framework.set(name)

    def __enter__(self) -> FrameworkScope:
        return self

    def __exit__(self, *exc: object) -> None:
        self.reset()

    def reset(self) -> None:
        if self._token is None:
            return
        try:
            _current_framework.reset(self._token)
        except ValueError:
            # ContextVar.reset() requires the SAME contextvars.Context the
            # matching .set() ran in -- confirmed live (govern.py's
            # Governor.authorize_tool(), reachable through an async
            # `@gov.tool` wrapper) that a caller spanning separate `await`
            # boundaries can genuinely close() this from a different
            # Context than open() ran in. Never let resetting a scope
            # raise into the caller's own code for that -- worse than a
            # stale token, same "never let a tagging failure break the
            # underlying instrumented call" principle
            # ObservationSpanProcessor.on_start() already applies.
            pass
        self._token = None


def set_current_framework(name: str) -> FrameworkScope:
    """Call as `with set_current_framework("maf"):` wrapping the block that
    opens a framework's own `parapetai.tool_call` span (e.g. maf.py's
    `with _tracer.start_as_current_span("parapetai.tool_call", ...)`).
    Any network child span opened inside that block is tagged with this
    framework name by ObservationSpanProcessor, if observation capture is
    enabled (enable_observation_capture()) -- a no-op otherwise, so this
    is safe to call unconditionally regardless of whether observation
    capture is turned on in this process.

    `name` should be one of "maf" | "adk" | "langgraph" | "governor" per
    §10.7's coverage matrix, but this module does not validate the value
    -- an adapter added later just works by picking its own string."""
    return FrameworkScope(name)


def current_framework() -> str | None:
    return _current_framework.get()


# --------------------------------------------------------------------------
# ObservedCall -- documents the shape ObservationSpanProcessor writes onto
# a span as attributes (auth-integrations.md §10.2). Not constructed and
# passed around as a real object anywhere in this module -- OTel spans are
# the actual wire format, this dataclass exists so the shape has one
# canonical, typed definition instead of being implicit in a pile of
# span.set_attribute() calls.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedCall:
    protocol: str  # "http"|"grpc"|"db" (Phase A, span-classified) | "mcp" (Phase B, direct)
    verb: str
    target: str
    destination: str | None
    framework: str | None
    args_shape: tuple[str, ...] = ()

    def attributes(self) -> dict[str, Any]:
        """The dict ObservationSpanProcessor actually writes onto a span,
        one `parapetai.observed.*` key per field -- `parapetai.observed`
        itself is the marker a control-plane ingester filters on to find
        these among every other span OTLP already carries."""
        attrs: dict[str, Any] = {
            "parapetai.observed": True,
            "parapetai.observed.protocol": self.protocol,
            "parapetai.observed.verb": self.verb,
            "parapetai.observed.target": self.target,
        }
        if self.destination:
            attrs["parapetai.observed.destination"] = self.destination
        if self.framework:
            attrs["parapetai.observed.framework"] = self.framework
        if self.args_shape:
            attrs["parapetai.observed.args_shape"] = self.args_shape
        return attrs


def bucket_key(agent_id: str, protocol: str, verb: str, target: str) -> str:
    """(agent_id, protocol, verb, target) -> a stable id. See this
    module's own docstring: MUST match whatever a Phase C control-plane
    implementation computes for the same tuple -- same algorithm choice
    and truncation length as parapetai_control.suggestions._suggestion_id
    on the control-plane side (sha256, first 16 hex chars), deliberately,
    so the convention is consistent across both repos even though nothing
    imports this function from there."""
    raw = f"{agent_id}\x00{protocol}\x00{verb}\x00{target}"
    return "ob-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Collection budget (§10.3/§10.17) -- server-controlled, via whatever the
# bundle-poll response's own "observation_collection" field says. A bucket
# collects by default (see this module's own top-of-file docstring and
# §10.3's "no chicken-and-egg start instruction" reasoning) -- this class
# only ever tracks the STOP half.
# --------------------------------------------------------------------------


class CollectionBudget:
    """Tracks which buckets this agent has been told to stop enriching.
    One instance per Bootstrap (see control_plane.py's
    enable_observation_capture() wiring) -- not global module state, since
    a process embedding more than one Bootstrap (unusual, but not
    disallowed) must not have one agent's saturation list suppress
    another's observations.

    Thread-safety: update_from_bundle_meta() is called from whichever
    thread runs the bundle-poll loop (run_bundle_poller's own thread);
    is_saturated() is called from request-handling threads via
    ObservationSpanProcessor.on_start(). Python's GIL makes a bare `set`
    swap (not in-place mutation) atomic enough for this: readers always
    see either the old or the new complete set, never a partially-updated
    one -- see update_from_bundle_meta()'s own comment.
    """

    def __init__(self) -> None:
        self._saturated: frozenset[str] = frozenset()

    def is_saturated(self, agent_id: str, protocol: str, verb: str, target: str) -> bool:
        return bucket_key(agent_id, protocol, verb, target) in self._saturated

    def update_from_bundle_meta(self, bundle: Mapping[str, Any]) -> None:
        """Wire this directly as the `on_bundle_meta` callback passed to
        run_bundle_poller()/poll_once() (control_plane.py) -- called once
        per poll cycle with that cycle's FULL bundle response. Reads
        `bundle["observation_collection"]["saturated_buckets"]` if
        present; anything else (key absent, wrong shape, an older control
        plane that doesn't send this field at all) leaves the budget
        object's state exactly as it already was -- a malformed or
        missing signal must never be treated as "everything is now
        saturated" (that would silently stop detection fleet-wide) NOR as
        "everything is now un-saturated" (that would silently blow past
        every cap this mechanism exists to enforce); the only safe
        response to "no valid instruction was received this cycle" is "do
        nothing this cycle."

        A full REPLACE of the saturated set (not a merge/union) on every
        valid response -- the control plane is authoritative and reports
        its current complete view each cycle, so a bucket that drops out
        of the list (an operator's "Resume collection", §10.3, or a
        scheduled recheck, §10.17) is honoured on the very next poll
        without this class needing a separate "un-saturate" method."""
        collection = bundle.get("observation_collection")
        if not isinstance(collection, Mapping):
            return
        buckets = collection.get("saturated_buckets")
        if not isinstance(buckets, Iterable) or isinstance(buckets, (str, bytes)):
            return
        try:
            self._saturated = frozenset(str(b) for b in buckets)
        except TypeError:
            # Genuinely unparseable entries -- same "do nothing this
            # cycle" response as a missing/malformed top-level shape.
            return


# Attribute keys this module treats as identifying a span's protocol,
# read defensively across semconv generations that have shipped in real
# opentelemetry-instrumentation-* releases -- same "don't trust one
# version's shape" discipline CLAUDE.md already states for cedarpy.
# Order matters within each tuple: first key present wins.
_HTTP_VERB_KEYS: tuple[str, ...] = ("http.request.method", "http.method")
_HTTP_TARGET_KEYS: tuple[str, ...] = ("url.path", "http.target", "url.full", "http.url")
_HTTP_DEST_KEYS: tuple[str, ...] = ("server.address", "net.peer.name", "http.host")
_DB_VERB_KEYS: tuple[str, ...] = ("db.operation", "db.system")
_DB_TARGET_KEYS: tuple[str, ...] = ("db.sql.table", "db.statement", "db.name", "db.namespace")
_DB_DEST_KEYS: tuple[str, ...] = ("server.address", "net.peer.name")
_RPC_SYSTEM_KEYS: tuple[str, ...] = ("rpc.system",)
_RPC_VERB_KEYS: tuple[str, ...] = ("rpc.method",)
_RPC_TARGET_KEYS: tuple[str, ...] = ("rpc.service",)
_RPC_DEST_KEYS: tuple[str, ...] = ("server.address", "net.peer.name")


def _first(attrs: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value:
            return str(value)
    return None


def _classify(attrs: Mapping[str, Any]) -> tuple[str, str, str, str | None] | None:
    """attrs -> (protocol, verb, target, destination), or None if this span
    carries none of the semantic-convention keys this module knows how to
    read -- e.g. the parapetai.tool_call/parapetai.model_call span itself,
    or a network call through a library corroboration.py doesn't
    instrument at all. Checked in this order (rpc, then db, then http)
    because a gRPC-over-HTTP2 span could in principle carry both rpc.* and
    some generic http.* attribute depending on instrumentor internals --
    rpc.system presence is the more specific signal when it exists."""
    if attrs.get("rpc.system"):
        verb = _first(attrs, _RPC_VERB_KEYS)
        target = _first(attrs, _RPC_TARGET_KEYS)
        if verb and target:
            return "grpc", verb, target, _first(attrs, _RPC_DEST_KEYS)
    if attrs.get("db.system"):
        verb = _first(attrs, _DB_VERB_KEYS) or "query"
        target = _first(attrs, _DB_TARGET_KEYS)
        if target:
            return "db", verb, target, _first(attrs, _DB_DEST_KEYS)
    verb = _first(attrs, _HTTP_VERB_KEYS)
    target = _first(attrs, _HTTP_TARGET_KEYS)
    if verb and target:
        return "http", verb, target, _first(attrs, _HTTP_DEST_KEYS)
    return None


try:
    # Real inheritance, not duck-typing: opentelemetry-sdk's own
    # SpanProcessor base class defines default no-op implementations of
    # whatever internal hooks the multiplexer (SynchronousMultiSpanProcessor)
    # calls beyond the four documented public methods -- confirmed live
    # that the installed SDK version also calls a private `_on_ending`
    # this class would otherwise need to reimplement, and get wrong again
    # on the SDK's next internal-hook addition. Subclassing means every
    # such hook this SDK version (or a future one) adds is inherited for
    # free. Wrapped in try/except, not a bare top-level import, so
    # `import parapetai_agent.observation` still succeeds in a process
    # with only the OTel API stub installed, not the SDK -- see
    # enable_observation_capture()'s own isinstance check for the actual
    # place that requires the real SDK to be present.
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover -- exercised only without opentelemetry-sdk installed
    _SpanProcessorBase = object  # type: ignore[assignment,misc]


class ObservationSpanProcessor(_SpanProcessorBase):
    """opentelemetry.sdk.trace.SpanProcessor subclass, registered by
    enable_observation_capture(). on_start() is where a corroboration
    instrumentor's child span already carries its own semantic-convention
    attributes (set at span-creation time by the instrumentor itself, so
    they're already present by the time any SpanProcessor's on_start
    fires -- confirmed against the installed opentelemetry-sdk's own
    Span.on_start()) -- this tags a subset of THOSE spans, never creates
    any of its own.

    Gated on current_framework() being set (i.e. a real span is starting
    INSIDE some framework's own `with set_current_framework(...):` block
    around its `parapetai.tool_call` span) -- not just on the span's own
    attribute shape. Without this gate, corroboration's process-wide httpx
    instrumentation means ANY http/db/grpc-shaped span gets tagged,
    including ones this SDK's own infrastructure creates that have
    nothing to do with a tool calling a vendor -- e.g.
    control_plane.py's own bundle-poll/heartbeat calls, were those ever
    NOT already suppressed via control_plane._suppressed_instrumentation()
    (a second, independent safeguard; this gate is the one that also
    covers any other non-tool-call caller this process might have, not
    only that one). A genuine tool call always runs inside the current
    framework's own tool_call span, so this correctly excludes nothing a
    real observation is supposed to capture."""

    def __init__(self, agent_id: str, budget: CollectionBudget) -> None:
        self._agent_id = agent_id
        self._budget = budget

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        if current_framework() is None:
            return
        attrs = getattr(span, "attributes", None) or {}
        classified = _classify(attrs)
        if classified is None:
            return
        protocol, verb, target, destination = classified
        if self._budget.is_saturated(self._agent_id, protocol, verb, target):
            return
        observed = ObservedCall(
            protocol=protocol,
            verb=verb,
            target=target,
            destination=destination,
            framework=current_framework(),
        )
        for key, value in observed.attributes().items():
            try:
                span.set_attribute(key, value)
            except Exception:  # noqa: BLE001 -- see module docstring: never let
                # a tagging failure break the underlying instrumented call.
                # A span attribute write should never raise in practice,
                # but this processor sits directly in the path of every
                # instrumented network call this process makes -- an
                # uncaught exception here would break real traffic for a
                # detection feature, which is strictly worse than a
                # missed observation.
                log.warning("observation_span_tag_failed", key=key)
                return

    # on_end/shutdown/force_flush: inherited from SpanProcessor's own
    # no-op defaults, unchanged -- attributes are written at on_start,
    # before the underlying call proceeds, and export is whatever
    # processor chain configure_otel() already set up; this class only
    # mutates a span in flight, it never exports or owns shutdown/flush.


_active_processor: ObservationSpanProcessor | None = None


def enable_observation_capture(
    agent_id: str, budget: CollectionBudget | None = None
) -> CollectionBudget:
    """Registers an ObservationSpanProcessor on the CURRENT TracerProvider
    -- same ordering requirement corroboration.py's own
    enable_http_corroboration() documents prominently: call this AFTER
    configure_otel() (or whatever else registers the real provider), not
    before, or every observation this process ever tags goes to a
    provider nothing exports from. Idempotent per agent_id -- calling
    again replaces the previously-registered processor's budget reference
    rather than stacking a second processor, so a caller re-bootstrapping
    in the same process doesn't double-tag every span.

    Returns the CollectionBudget in use (the one passed in, or a freshly
    created one) -- wire its `.update_from_bundle_meta` method as the
    `on_bundle_meta` callback on run_bundle_poller()/poll_once()
    (control_plane.py) so §10.3/§10.17's server-issued saturation
    instructions actually reach it.
    """
    global _active_processor
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider as _SDKTracerProvider

    resolved_budget = budget if budget is not None else CollectionBudget()
    provider = _trace.get_tracer_provider()
    if not isinstance(provider, _SDKTracerProvider):
        # Same defensive stance as corroboration.py's own "call after
        # configure_otel()" note -- rather than silently doing nothing,
        # this is loud in the log (not raised: a caller who genuinely
        # wants to defer OTel setup to later in startup shouldn't have
        # this crash the process), since a silently-inert observation
        # capture would be a confusing thing to debug later.
        log.warning("observation_capture_no_sdk_tracer_provider")
        return resolved_budget
    processor = ObservationSpanProcessor(agent_id, resolved_budget)
    provider.add_span_processor(processor)
    _active_processor = processor
    log.info("observation_capture_enabled", agent_id=agent_id)
    return resolved_budget


def observation_capture_enabled() -> bool:
    return _active_processor is not None


# --------------------------------------------------------------------------
# Phase B (§10.16): the gateway's MCP path. Unlike Phase A's corroboration-
# piggybacked spans (a real outbound network call's OWN span, classified
# and tagged after the fact by ObservationSpanProcessor because this
# module never knows in advance what shape it will be), the gateway
# parses an inbound MCP tools/call request off the wire directly --
# MCPParser already hands it tool_name/tool_args with certainty before
# this function is ever called. There is no ambient span to tag, so this
# constructs and exports a standalone one instead.
# --------------------------------------------------------------------------


def emit_observed_span(
    agent_id: str, budget: CollectionBudget, observed: ObservedCall, *, tracer: Any
) -> None:
    """Direct-construction counterpart to ObservationSpanProcessor.on_start,
    for a caller that already knows an ObservedCall's exact shape instead
    of needing to infer it from semantic-convention attributes. Opens one
    standalone, parent-less span carrying the identical parapetai.observed.*
    attributes ObservationSpanProcessor writes onto a piggybacked span, so
    the control plane's ingestion side (parapetai_control.otlp.
    ingest_traces) needs no protocol-specific branch to find them --
    "protocol" on the emitted attributes is simply whatever `observed.
    protocol` says (`"mcp"` for the gateway's own caller today).

    Same budget check as ObservationSpanProcessor.on_start (a saturated
    bucket is a silent no-op, not an error) and the same "never let a
    tagging failure break the underlying call" stance: `tracer` is
    expected to be a real opentelemetry.trace.Tracer, but if span creation
    or an attribute write raises for any reason, this logs and returns
    rather than propagating into the caller's own request-handling path --
    exactly the same risk profile ObservationSpanProcessor.on_start
    documents for itself, just without an SDK-internal call stack backing
    it (this IS the caller-facing surface for that one).
    """
    if budget.is_saturated(agent_id, observed.protocol, observed.verb, observed.target):
        return
    try:
        with tracer.start_as_current_span("parapetai.observed_call") as span:
            for key, value in observed.attributes().items():
                span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 -- see this function's own docstring.
        log.warning("observed_span_emit_failed", protocol=observed.protocol, verb=observed.verb)


# --------------------------------------------------------------------------
# In-process MCP client observation. A THIRD capture path, alongside
# Phase A's span-classified ObservationSpanProcessor and Phase B's
# gateway-side direct construction -- for the case neither covers: an
# in-process tool that is ITSELF an MCP client, talking to a remote MCP
# server directly (no gateway in the path at all). docs/reference/
# vendor-scope-permission.md documents the finding that made this
# necessary: agent_framework.MCPTool (and its Stdio/StreamableHTTP/
# Websocket subclasses) dispatch a tool call through a persistent
# background "lifecycle owner" asyncio.Task, created once and reused for
# the tool's whole lifetime -- the real network call for ANY given
# tools/call therefore runs detached from whatever span/
# current_framework() was ambient for that specific call, which is
# exactly why ObservationSpanProcessor's corroboration-piggybacked
# approach (Phase A) structurally cannot see it: there is no reliable
# ambient tool_call scope to gate on.
#
# The fix sidesteps that problem entirely rather than solving it: instead
# of inferring a call's shape from span attributes, this patches
# mcp.client.session.ClientSession.call_tool directly -- the ONE place,
# confirmed by inspecting both agent_framework's and google-adk's own MCP
# tool implementations, that EVERY in-process MCP client integration this
# SDK has verified funnels a `tools/call` request through, regardless of
# which higher-level framework wrapper (or persistent-task architecture)
# sits above it. `name`/`arguments` arrive as plain Python values here,
# before any JSON-RPC serialization even happens -- no body-parsing
# required, unlike the gateway's own MCPParser (which has to parse wire
# bytes because it never sees the caller's native call).
# --------------------------------------------------------------------------

_mcp_active_agent_id: str | None = None
_mcp_active_budget: CollectionBudget | None = None
_mcp_original_call_tool: Any | None = None
_mcp_original_initialize: Any | None = None
_mcp_original_list_tools: Any | None = None
# Transport-connector originals, keyed by (module_path, attr_name) -- see
# _patch_transport_connectors()/_unpatch_transport_connectors() below.
_mcp_original_transport_connectors: dict[tuple[str, str], Any] = {}


def _host_from_url(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Real MCP server URL capture. _emit_mcp_initialize_span()'s own docstring
# used to record why this was believed unreachable: `ClientSession` is
# constructed from raw read/write streams (verified against the installed
# `mcp` package's __init__ signature), not a URL -- only whatever built the
# underlying transport ever holds that, and patching ClientSession itself
# (the class-level patch above, chosen for its import-order independence)
# structurally cannot see it.
#
# The fix: patch the TRANSPORT CONNECTORS themselves --
# mcp.client.sse.sse_client, mcp.client.streamable_http.streamablehttp_client
# (and the streamable_http_client alias some callers use), and
# mcp.client.websocket.websocket_client -- confirmed directly against the
# installed `mcp` package that all four are `@asynccontextmanager`-wrapped
# async generator functions taking `url` as their first argument. Each
# wrapped connector stashes that url in a ContextVar for the duration of the
# `async with ...:` block; `_patched_initialize` below reads it while still
# inside that same block (the whole `async with sse_client(url) as (r, w):
# session = ClientSession(r, w); await session.initialize()` sequence runs
# in one coroutine/task, and a ContextVar set earlier in that chain is still
# visible later in it -- the same propagation set_current_framework() above
# already relies on, not a new assumption).
#
# IMPORT-ORDER CAVEAT, stated plainly rather than left implicit: unlike the
# ClientSession class-level patch (real inheritance, works regardless of
# when it's applied relative to any session construction), a caller that
# does `from mcp.client.sse import sse_client` at ITS OWN module top level
# binds that name at THAT module's import time -- confirmed against
# google.adk.tools.mcp_tool.mcp_session_manager, which does exactly this.
# If that ADK module is already imported before enable_mcp_observation()
# runs, ADK's own bound reference is the ORIGINAL, unpatched function, and
# the URL is not captured for THAT session (silently -- initialize() still
# emits its span, just without parapetai.mcp.server_url). In the normal
# embedding order -- Bootstrap/GovernedRunner construction (which calls
# enable_mcp_observation()) before the embedding app constructs its own MCP
# tools -- this is a non-issue: confirmed live that a bare `import
# google.adk` does NOT eagerly import mcp_session_manager, so the patch is
# already in place by the time ADK's own lazy `from mcp.client.sse import
# sse_client` runs and resolves to the (by-then-patched) module attribute.
# agent_framework's own MCP tool implementation is unaffected either way --
# confirmed it imports streamablehttp_client/websocket_client with a LOCAL
# import inside the method that uses them, a fresh attribute lookup on
# every call, so it is never sensitive to import order at all.
_TRANSPORT_URL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "parapetai_agent_observation_current_transport_url", default=None
)

# (module dotted path, attribute name) for every transport connector known
# to take `url` as its first argument -- confirmed against the installed
# `mcp` package's own signatures, not assumed. `mcp.client.stdio.stdio_client`
# is deliberately excluded: it takes a command/args, not a URL, so there is
# no URL to capture for a locally-spawned server.
_TRANSPORT_CONNECTOR_TARGETS: tuple[tuple[str, str], ...] = (
    ("mcp.client.sse", "sse_client"),
    ("mcp.client.streamable_http", "streamablehttp_client"),
    ("mcp.client.streamable_http", "streamable_http_client"),
    ("mcp.client.websocket", "websocket_client"),
)


def _wrap_transport_connector(original: Any) -> Any:
    """Wraps one url-taking `@asynccontextmanager` transport connector so
    entering it publishes `url` to `_TRANSPORT_URL` for the duration of the
    `async with` block, then restores whatever was there before (never just
    clearing to None -- a nested/second connection inside the same task,
    however unusual, must not have its OWN url wiped by an outer one's
    exit). Forwards args/kwargs and the yielded value completely opaquely
    (whatever tuple arity the real connector yields -- 2 for sse_client/
    websocket_client, 3 for streamablehttp_client -- passes through
    unexamined), so this never needs updating if a future `mcp` release
    changes what a connector yields."""

    @functools.wraps(original)
    @asynccontextmanager
    async def _wrapped(url: str, *args: Any, **kwargs: Any) -> Any:
        token = _TRANSPORT_URL.set(url)
        try:
            async with original(url, *args, **kwargs) as value:
                yield value
        finally:
            _TRANSPORT_URL.reset(token)

    return _wrapped


def _patch_transport_connectors() -> None:
    """Best-effort per target: some `mcp` package versions/installs may not
    ship every one of these submodules (e.g. websocket support pulled via
    an extra); a missing one must not prevent patching the others. Already-
    patched targets (module attr is already one of our own `_wrapped`
    closures, i.e. present in `_mcp_original_transport_connectors`) are
    skipped -- same idempotent-re-enable posture as call_tool/initialize/
    list_tools above."""
    for module_path, attr_name in _TRANSPORT_CONNECTOR_TARGETS:
        key = (module_path, attr_name)
        if key in _mcp_original_transport_connectors:
            continue
        try:
            module = __import__(module_path, fromlist=[attr_name])
            original = getattr(module, attr_name)
        except (ImportError, AttributeError):
            continue
        _mcp_original_transport_connectors[key] = original
        setattr(module, attr_name, _wrap_transport_connector(original))


def _unpatch_transport_connectors() -> None:
    for (module_path, attr_name), original in _mcp_original_transport_connectors.items():
        try:
            module = __import__(module_path, fromlist=[attr_name])
            setattr(module, attr_name, original)
        except ImportError:
            pass
    _mcp_original_transport_connectors.clear()


# --------------------------------------------------------------------------
# Session-level MCP telemetry: the handshake (initialize) and the server's
# own tool catalog (list_tools) -- metadata about the CONNECTION, not a
# specific call, so each gets its own standalone span rather than being
# shoehorned into ObservedCall's (protocol, verb, target) call-shape model.
# Deliberately NOT gated by CollectionBudget: that budget exists to cap
# repeated-call-shape volume for VSP detection sampling (§10.3), a
# different concern from a handshake that happens once (or a tools/list
# that happens rarely) per session -- gating here on _mcp_active_agent_id
# alone (observation enabled at all) is the right granularity.
#
# None of these fields are content_bearing under otel/openinference.py's
# meaning of that word (CLAUDE.md invariant 10): they are the MCP SERVER's
# own declared, static metadata -- its name/version/capabilities/tool
# catalog/usage instructions -- never a prompt, a tool argument value, or a
# model response. Same category openinference.py already puts
# TOOL_DESCRIPTION in (free text, but server/tool-authored, not user- or
# model-authored), so these are emitted unconditionally, not gated behind
# PARAPETAI_OTEL_LOG_CONTENT.
#
# These are Parapet-invented attribute names, not part of the OpenInference
# spec -- deliberately NOT added to otel/openinference.py's ATTRS registry,
# which is scoped to that external vocabulary specifically (see that
# module's own docstring). Kept here alongside parapetai.observed.* for the
# same reason those are: this module already owns its own small attribute
# namespace for MCP-specific telemetry.
# --------------------------------------------------------------------------

_MCP_INIT_SPAN_NAME = "parapetai.mcp_session_initialize"
_MCP_LIST_TOOLS_SPAN_NAME = "parapetai.mcp_list_tools"

# ServerCapabilities' own top-level field names (mcp.types.ServerCapabilities,
# verified directly against the installed `mcp` package, not assumed) --
# what the SERVER declared it supports. Distinct from ClientCapabilities
# (roots/sampling/elicitation): those are computed and SENT by
# ClientSession.initialize() itself, never stored back onto the session, so
# they are not recoverable here after the fact without re-deriving
# SDK-internal logic that could silently drift from a future `mcp` release
# -- reporting only what the RESULT actually carries is the honest choice.
_SERVER_CAPABILITY_FIELDS = (
    "prompts",
    "resources",
    "tools",
    "logging",
    "completions",
    "experimental",
)


def _server_capabilities_dict(capabilities: Any) -> dict[str, bool]:
    """Which top-level ServerCapabilities groups the server declared, as a
    flat {name: True} map -- booleans, not the sub-objects themselves
    (each carries provider-specific extras -- e.g. tools.listChanged --
    with no single stable shape worth committing to here). Read by
    getattr, defensively, same discipline CLAUDE.md's cedarpy section
    already applies to a different external library's response shape."""
    if capabilities is None:
        return {}
    return {
        name: True
        for name in _SERVER_CAPABILITY_FIELDS
        if getattr(capabilities, name, None) is not None
    }


def _emit_mcp_initialize_span(session: Any, result: Any) -> None:
    """The MCP handshake's own vendor-identifying and capability-
    negotiation fields, as their own standalone span -- protocolVersion,
    the server's declared capabilities, serverInfo, instructions, and (when
    the session was constructed with one) our own clientInfo. Never raises:
    call sites wrap this the same "tagging must not break the real call"
    way emit_observed_span() already documents for itself.

    `parapetai.mcp.server_url` -- the REAL endpoint this session actually
    connected to -- is read from `self._parapetai_server_url`, cached by
    _patched_initialize() from `_TRANSPORT_URL` (see the transport-
    connector patching block above): the one field genuinely NOT reachable
    from `ClientSession` itself (constructed from raw read/write streams,
    verified directly against the installed package's __init__ signature)
    without that separate patch. `server_website_url` below is a DIFFERENT,
    weaker signal -- the server's own self-declared serverInfo.websiteUrl
    (a marketing page, not necessarily even the same host as the actual API
    endpoint) -- kept alongside, not replaced by, the real URL: a control
    plane consumer should prefer server_url when present and only fall back
    to server_website_url (e.g. for a stdio-transport session, which has no
    URL at all) when it isn't."""
    server_info = getattr(result, "serverInfo", None)
    client_info = getattr(session, "_client_info", None)
    attrs: dict[str, Any] = {
        "parapetai.mcp.protocol_version": getattr(result, "protocolVersion", None) or "",
        "parapetai.mcp.server_capabilities": json.dumps(
            _server_capabilities_dict(getattr(result, "capabilities", None))
        ),
    }
    server_url = getattr(session, "_parapetai_server_url", None)
    if server_url:
        attrs["parapetai.mcp.server_url"] = server_url
    if server_info is not None:
        attrs["parapetai.mcp.server_name"] = getattr(server_info, "name", None) or ""
        attrs["parapetai.mcp.server_version"] = getattr(server_info, "version", None) or ""
        website = getattr(server_info, "websiteUrl", None)
        if website:
            attrs["parapetai.mcp.server_website_url"] = website
    if client_info is not None:
        attrs["parapetai.mcp.client_name"] = getattr(client_info, "name", None) or ""
        attrs["parapetai.mcp.client_version"] = getattr(client_info, "version", None) or ""
    instructions = getattr(result, "instructions", None)
    if instructions:
        attrs["parapetai.mcp.instructions"] = instructions
    with _tracer.start_as_current_span(_MCP_INIT_SPAN_NAME) as span:
        for key, value in attrs.items():
            if value != "":
                span.set_attribute(key, value)


def _emit_mcp_list_tools_span(session: Any, result: Any) -> None:
    """The server's own tool catalog (name/description/inputSchema per
    tool) as one span -- field names AND values here, unlike
    parapetai.observed.*'s args_shape (finding #10's "field names only"
    convention is about a CALL's arguments, real user/task data; a tool's
    own declared schema is the server's static metadata, the same category
    tool.description already sits in). Never raises -- see
    _emit_mcp_initialize_span's own docstring for why."""
    tools = getattr(result, "tools", None) or []
    catalog = [
        {
            "name": getattr(tool, "name", None),
            "description": getattr(tool, "description", None),
            "input_schema": getattr(tool, "inputSchema", None),
        }
        for tool in tools
    ]
    with _tracer.start_as_current_span(_MCP_LIST_TOOLS_SPAN_NAME) as span:
        span.set_attribute(
            "parapetai.mcp.server_name", getattr(session, "_parapetai_server_name", None) or "mcp"
        )
        span.set_attribute("parapetai.mcp.tools", json.dumps(catalog))


def enable_mcp_observation(
    agent_id: str, budget: CollectionBudget | None = None
) -> CollectionBudget | None:
    """Patches `mcp.client.session.ClientSession` (class-level, so every
    instance any integration creates is covered -- agent_framework's
    MCPTool composes one internally, as does google.adk's own MCP tool
    support; confirmed against both installed packages directly, not
    assumed) so an in-process MCP client tool call is observed with
    certainty, the same "already know the shape, no attribute-guessing
    needed" principle emit_observed_span()/Phase B already use.

    Three methods are wrapped:
      - `initialize()`: best-effort caches the remote server's own
        declared `serverInfo.name` (and `websiteUrl`'s host, if given) on
        the session instance -- the MCP handshake's own vendor-identifying
        fields, read once per session rather than on every call -- and
        emits the full handshake (protocolVersion/capabilities/serverInfo/
        clientInfo/instructions) as its own span, see
        _emit_mcp_initialize_span().
      - `list_tools()`: emits the server's own declared tool catalog
        (name/description/inputSchema per tool) as its own span, see
        _emit_mcp_list_tools_span() -- whatever the caller's own code
        already does with the result is untouched; this only observes it.
      - `call_tool(name, arguments, ...)`: emits an ObservedCall built
        directly from `name`/`arguments` -- no span involved at all for
        classification (though `current_framework()` is still attached
        as an OPTIONAL tag, best-effort, when one happens to be
        ambient) -- before invoking the real call. By the time this method
        is ever reached, Cedar has already allowed the call: a denial
        raises inside the framework adapter's own wrap_tool_call/
        before_tool_callback hook, upstream of wherever a tool's own code
        (which is what eventually calls `session.call_tool`) ever runs.

    Returns the CollectionBudget in use (the one passed in, shared with
    whatever called this -- see governance_runtime.enable_automatic_
    detection(), which passes the SAME budget enable_observation_capture()
    already returned, so both capture paths honor one saturation state),
    or None if the `mcp` package isn't installed at all -- not every
    embedder using this SDK has an MCP tool in their agent, so this must
    not become a hard dependency or raise when it's absent.

    Idempotent, same "replace the active reference, don't stack a second
    patch" semantics as enable_observation_capture(): calling again with a
    different agent_id/budget re-targets the ALREADY-patched methods
    (module-level state, read fresh on every call_tool invocation) rather
    than patching twice. A process embedding more than one Bootstrap
    shares one process-wide patch target the same way it shares one
    process-wide httpx/grpc instrumentation target via corroboration.py --
    the last caller's agent_id/budget wins, a pre-existing class of
    trade-off, not a new one this introduces."""
    global \
        _mcp_active_agent_id, \
        _mcp_active_budget, \
        _mcp_original_call_tool, \
        _mcp_original_initialize, \
        _mcp_original_list_tools
    try:
        from mcp.client.session import ClientSession
    except ImportError:
        log.info("mcp_observation_unavailable", reason="mcp package not installed")
        return None

    resolved_budget = budget if budget is not None else CollectionBudget()
    _mcp_active_agent_id = agent_id
    _mcp_active_budget = resolved_budget

    if _mcp_original_call_tool is not None:
        log.info("mcp_observation_enabled", agent_id=agent_id)
        return resolved_budget

    _mcp_original_call_tool = ClientSession.call_tool
    _mcp_original_initialize = ClientSession.initialize
    _mcp_original_list_tools = ClientSession.list_tools
    original_call_tool = _mcp_original_call_tool
    original_initialize = _mcp_original_initialize
    original_list_tools = _mcp_original_list_tools

    @functools.wraps(original_initialize)
    async def _patched_initialize(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Read BEFORE awaiting the real initialize() -- still inside the
        # same `async with sse_client(url)/streamablehttp_client(url):`
        # block that set this, per the transport-connector patching block's
        # own docstring on why that ordering is safe to rely on.
        transport_url = _TRANSPORT_URL.get()
        result = await original_initialize(self, *args, **kwargs)
        try:
            if transport_url:
                self._parapetai_server_url = transport_url
            server_info = getattr(result, "serverInfo", None)
            if server_info is not None:
                self._parapetai_server_name = getattr(server_info, "name", None)
                website = getattr(server_info, "websiteUrl", None)
                # The REAL connection host (from the transport url, when we
                # have one) is ground truth -- confirmed always accurate,
                # since it's literally what this session dialed. Only fall
                # back to the server's self-declared websiteUrl (a
                # marketing page, not guaranteed to share a host with the
                # actual API endpoint) when there's no transport url at all
                # (a stdio-transport session, or ADK's own import-order
                # caveat above).
                self._parapetai_server_host = (
                    _host_from_url(transport_url)
                    if transport_url
                    else (_host_from_url(website) if website else None)
                )
            if _mcp_active_agent_id is not None:
                _emit_mcp_initialize_span(self, result)
        except Exception:  # noqa: BLE001 -- never let tagging break a real initialize() call.
            log.warning("mcp_observation_initialize_tag_failed")
        return result

    @functools.wraps(original_list_tools)
    async def _patched_list_tools(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_list_tools(self, *args, **kwargs)
        if _mcp_active_agent_id is not None:
            try:
                _emit_mcp_list_tools_span(self, result)
            except Exception:  # noqa: BLE001 -- never let tagging break a real list_tools() call.
                log.warning("mcp_observation_list_tools_tag_failed")
        return result

    @functools.wraps(original_call_tool)
    async def _patched_call_tool(
        self: Any, name: str, arguments: dict[str, Any] | None = None, *args: Any, **kwargs: Any
    ) -> Any:
        if _mcp_active_agent_id is not None and _mcp_active_budget is not None:
            try:
                observed = ObservedCall(
                    protocol="mcp",
                    verb=name,
                    target=getattr(self, "_parapetai_server_name", None) or "mcp",
                    destination=getattr(self, "_parapetai_server_host", None),
                    framework=current_framework(),
                    args_shape=tuple(sorted(arguments.keys()))
                    if isinstance(arguments, dict)
                    else (),
                )
                emit_observed_span(
                    _mcp_active_agent_id, _mcp_active_budget, observed, tracer=_tracer
                )
            except Exception:  # noqa: BLE001 -- never let tagging break a real tool call.
                log.warning("mcp_observation_call_tool_tag_failed", tool_name=name)
        return await original_call_tool(self, name, arguments, *args, **kwargs)

    ClientSession.initialize = _patched_initialize  # type: ignore[method-assign]
    ClientSession.call_tool = _patched_call_tool  # type: ignore[method-assign]
    ClientSession.list_tools = _patched_list_tools  # type: ignore[method-assign]
    # See the transport-connector patching block's own docstring for what
    # this adds (the real server URL) and its import-order caveat.
    _patch_transport_connectors()
    log.info("mcp_observation_enabled", agent_id=agent_id)
    return resolved_budget


def disable_mcp_observation() -> None:
    """Reverses enable_mcp_observation() -- real, exported functionality,
    same reason corroboration.py's own disable_http_corroboration() is:
    a test suite that reconfigures between cases needs a genuine restore,
    not just a state flag, since ClientSession.call_tool/initialize are
    patched at the CLASS level and would otherwise leak into whatever
    test or process runs next. Safe to call even if nothing was ever
    enabled."""
    global \
        _mcp_active_agent_id, \
        _mcp_active_budget, \
        _mcp_original_call_tool, \
        _mcp_original_initialize, \
        _mcp_original_list_tools
    if _mcp_original_call_tool is not None:
        try:
            from mcp.client.session import ClientSession

            ClientSession.call_tool = _mcp_original_call_tool  # type: ignore[method-assign]
            ClientSession.initialize = _mcp_original_initialize  # type: ignore[method-assign,assignment]
            ClientSession.list_tools = _mcp_original_list_tools  # type: ignore[method-assign,assignment]
        except ImportError:
            pass
    _unpatch_transport_connectors()
    _mcp_active_agent_id = None
    _mcp_active_budget = None
    _mcp_original_call_tool = None
    _mcp_original_initialize = None
    _mcp_original_list_tools = None


def mcp_observation_enabled() -> bool:
    return _mcp_original_call_tool is not None
