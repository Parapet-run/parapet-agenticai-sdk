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
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

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
    any of its own."""

    def __init__(self, agent_id: str, budget: CollectionBudget) -> None:
        self._agent_id = agent_id
        self._budget = budget

    def on_start(self, span: Any, parent_context: Any = None) -> None:
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
