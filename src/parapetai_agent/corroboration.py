"""auth-integrations.md §7, Tier 1: per-library OTel auto-instrumentation
for observed-vs-declared corroboration -- does a tool's real network
traffic look like what it declared via vendor_calls.declare_vendor_call?

WHAT THIS MODULE DOES AND DOES NOT DO. It turns on real OpenTelemetry
auto-instrumentation for the common HTTP/gRPC client libraries, so a real
network call a tool makes during its own execution emits a real child
span -- nested under whichever adapter's own `parapetai.tool_call` span is
current, since all three adapters already wrap call_next() in
`tracer.start_as_current_span("parapetai.tool_call")`, and OTel's context
propagation attaches a new span to whatever span is ambient when it
starts. That's the ENTIRE job here: making the signal exist and land in
the right place in the trace tree. It does NOT compare the resulting
span's host/method against a tool's declared crud_action, and does NOT
revoke a classification or re-queue anything for triage -- both spans (the
tool_call span and its new network child) already flow to the control
plane over the existing OTLP `/v1/traces` pipe once configure_otel() is
wired up, so that comparison can be done from the correlated,
already-ingested span data (parent_span_id links a child straight back to
its tool_call span, and that tool_call span's own attributes/its
correlated decision record already carry context.crud_action) without any
new SDK-side machinery. Building that comparison and its enforcement
consequence is deliberately left as later, separate work -- it is a
control-plane analysis problem once this signal exists, not something
that needs to happen synchronously inside a customer's own process.

HOW A LIBRARY THAT WASN'T INSTALLED IS HANDLED. Each candidate below is
the OFFICIAL opentelemetry-python-contrib instrumentor for one transport
library. Calling `.instrument()` on one whose target library (e.g.
`httpx`) is not installed in the CALLING APPLICATION's environment does
NOT raise -- `BaseInstrumentor.instrument()` runs its own dependency check
first (`importlib.metadata.version(name)`, catching `PackageNotFoundError`)
and simply returns without calling `_instrument()` if the target is
missing, logging an ERROR-level line through the stdlib `logging` module
as it does. That per-library ERROR line is suppressed here (this module's
own structlog summary line replaces it) because it is the COMMON case, not
a fault: a typical agent process uses one or two of these transports, not
all five, and five ERROR lines on every startup for the four it doesn't
use would read as something broken.

COVERAGE THROUGH A VENDOR SDK. These instrumentors patch the TRANSPORT
LIBRARY's own methods (e.g. `httpx.HTTPTransport.handle_request`), not
whatever application code calls them -- so a vendor SDK built on top of an
instrumented transport (simple-salesforce -> requests; boto3 -> botocore
-> urllib3; any generated gRPC client -> grpc.Channel) is covered with NO
awareness of that SDK's existence: however many layers of wrapping sit on
top, the call is observed at the bottom, where it actually leaves the
process. The one real limitation is context propagation, not library
detection: the emitted span attaches to whichever OTel context is current
at the moment the call happens, which works automatically for a tool
running synchronously (or in the same asyncio task) as its own tool_call
span, but is NOT guaranteed if a vendor SDK hands the actual I/O off to a
thread pool or subprocess of its own without copying that context across
-- a real, known class of gap in OTel context propagation generally, not
specific to this module, and not solved here.

ORDERING MATTERS: CALL THIS AFTER YOUR TRACER PROVIDER IS SET, NOT BEFORE.
Confirmed live, not assumed: each contrib instrumentor resolves and
CAPTURES a real `Tracer` reference inside its own `_instrument()` (typically
`get_tracer(__name__, tracer_provider=tracer_provider or
trace.get_tracer_provider())`), unlike this SDK's own adapters, whose
module-level `_tracer = trace.get_tracer(__name__)` is a lazy proxy that
re-resolves against whatever provider is globally current at EVERY
`start_as_current_span()` call. A contrib instrumentor does not re-resolve
later -- if `enable_http_corroboration()` runs before `configure_otel()`
(or any other call that registers the real TracerProvider), every span it
ever emits goes to whatever no-op/default tracer was active at that
earlier moment, permanently, until the process re-instruments. Call
`configure_otel()` (or otherwise register the real provider) FIRST.

WHAT "NO SIGNAL" MEANS. A tool call that uses a transport library outside
this curated set (or Tier 2/3's territory: a C-extension driver, or no
network call at all) simply produces no child span -- indistinguishable,
at this tier, from a call that matched its declaration correctly. Absence
of a mismatch signal is not proof of a match; this module does not attempt
to paper over that distinction (e.g. by inventing a tri-state "checked but
clean" marker) because nothing here can tell "no supported library was
used" apart from "no network call happened at all" or "the correlation
silently failed" -- Tiers 2/3 exist specifically to shrink this blind
spot, not this one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Candidate:
    module: str
    class_name: str
    library: str  # human-readable, for the summary log only


# Deliberately HTTP/gRPC transports only -- see this module's own
# docstring for why the database instrumentors opentelemetry-python-contrib
# also ships (-psycopg2/-pymongo/-redis/-sqlalchemy) are out of scope here.
_CANDIDATES: tuple[_Candidate, ...] = (
    _Candidate("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", "httpx"),
    _Candidate("opentelemetry.instrumentation.requests", "RequestsInstrumentor", "requests"),
    _Candidate("opentelemetry.instrumentation.urllib3", "URLLib3Instrumentor", "urllib3"),
    _Candidate(
        "opentelemetry.instrumentation.aiohttp_client", "AioHttpClientInstrumentor", "aiohttp"
    ),
    _Candidate("opentelemetry.instrumentation.grpc", "GrpcInstrumentorClient", "grpc"),
)

_enabled = False


def http_corroboration_enabled() -> bool:
    """True once enable_http_corroboration() has been called in this
    process, regardless of how many (if any) candidates actually
    activated -- a caller that only wants to know "was this turned on"
    rather than the per-library detail enable_http_corroboration() itself
    returns."""
    return _enabled


def enable_http_corroboration() -> dict[str, bool]:
    """Turns on OTel auto-instrumentation for every candidate transport
    library actually installed in this process. Idempotent and safe to
    call more than once (from more than one build_middleware()/
    build_plugin() call in the same process, the same idempotency
    precedent those functions' own registries already set) -- relies on
    BaseInstrumentor's own re-instrument guard (logs a warning, no-ops)
    rather than adding a second one here.

    Returns {library_name: whether it actually activated} for every
    candidate, in the same order they're tried -- False means either the
    instrumentor package itself isn't installed (this extra wasn't
    installed, or was installed partially) or its target library isn't
    installed in the calling application; both are the ordinary,
    unremarkable case for at least some entries on every real deployment,
    not something a caller needs to treat as an error.
    """
    global _enabled
    result: dict[str, bool] = {}
    # See this module's own docstring: BaseInstrumentor.instrument() logs
    # an ERROR-level line, via stdlib logging, for every candidate whose
    # target library is absent -- the common case, not a fault. Silenced
    # for the duration of this call only; restored immediately after so a
    # caller's own use of this logger elsewhere is unaffected.
    otel_logger = logging.getLogger("opentelemetry.instrumentation.instrumentor")
    previous_level = otel_logger.level
    otel_logger.setLevel(logging.CRITICAL)
    try:
        for candidate in _CANDIDATES:
            result[candidate.library] = _try_instrument(candidate)
    finally:
        otel_logger.setLevel(previous_level)
    _enabled = True
    log.info(
        "http_corroboration_enabled",
        instrumented=sorted(k for k, v in result.items() if v),
        skipped=sorted(k for k, v in result.items() if not v),
    )
    return result


def disable_http_corroboration() -> None:
    """Reverses enable_http_corroboration() -- calls `.uninstrument()` on
    every candidate that's currently active. Real, exported functionality
    (a customer's own test suite needs exactly this to avoid the same
    stale-tracer trap this module's own tests hit: see the "ORDERING
    MATTERS" section of this module's docstring -- an already-instrumented
    library keeps emitting to whatever TracerProvider was current at
    instrument() time, so a test that reconfigures OTel between cases
    needs to uninstrument first, or the new configuration is silently
    never observed), not merely a test fixture living here by accident.
    Safe to call even if nothing was ever enabled (each instrumentor's own
    `is_instrumented_by_opentelemetry` guard makes `.uninstrument()` a
    no-op in that case)."""
    global _enabled
    for candidate in _CANDIDATES:
        try:
            module = __import__(candidate.module, fromlist=[candidate.class_name])
            instrumentor_cls = getattr(module, candidate.class_name)
        except ImportError:
            continue
        instrumentor = instrumentor_cls()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    _enabled = False


def _try_instrument(candidate: _Candidate) -> bool:
    try:
        module = __import__(candidate.module, fromlist=[candidate.class_name])
        instrumentor_cls = getattr(module, candidate.class_name)
    except ImportError:
        # The instrumentor PACKAGE itself isn't importable -- this
        # extra wasn't installed at all, or was installed against an
        # environment missing one of its own entries. Same observable
        # outcome as the target-library-missing case _instrument() below
        # handles: this one transport just isn't covered here.
        return False
    instrumentor = instrumentor_cls()
    if instrumentor.is_instrumented_by_opentelemetry:
        return True
    instrumentor.instrument()
    return bool(instrumentor.is_instrumented_by_opentelemetry)
