"""Framework-agnostic runtime plumbing shared by every in-process framework
integration this package ships (parapetai_agent.maf, parapetai_agent.adk,
...): OpenTelemetry wiring (spans + LogRecords + Azure Monitor/OTLP export),
the rotating-file JSON audit sink, the "decision" audit event itself, and a
few small Cedar-decision helpers (unresolved-ALTER / content-check-failure
synthetic denials).

Originally lived inside parapetai_agent/maf.py, module-private, even though
none of it depends on agent_framework -- it's OTel SDK + structlog + Path +
parapetai_agent.policy (already framework-agnostic). Extracted here for a
genuine correctness reason, not just tidiness: configure_otel() sets a
PROCESS-WIDE TracerProvider/LoggerProvider (OTel's own global-registration
model), so if parapetai_agent.maf and parapetai_agent.adk each carried their
own private copy of this state, a process using BOTH integrations (or
switching between them) could end up with two independent "is OTel already
configured?" checks disagreeing with each other -- one module happily
calling configure_otel() a second time, silently discarding whichever
provider the other one already registered. One shared module means one
shared answer to "has this process already configured OTel".

Each framework module still gets its own OTel tracer
(`trace.get_tracer(__name__)`, standard per-instrumentation-scope practice)
-- only the process-wide TracerProvider/LoggerProvider registration and the
"decision" audit event itself are shared here.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import logging
import logging.handlers
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import structlog
from opentelemetry import trace
from opentelemetry._logs import Logger as _OtelLogger
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import ConsoleLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

import parapetai_agent.policy as parapetai_agent_policy
from parapetai_agent._exceptions import GovernanceDenied as GovernanceDenied
from parapetai_agent.policy.engine import Decision
from parapetai_agent.policy.hooks import content_free
from parapetai_agent.providers.parsers import Snapshot

log = structlog.get_logger(__name__)

# None until configure_otel() runs; _emit_otel_decision() no-ops until then.
_otel_logger: _OtelLogger | None = None
# Held so flush_otel()/the atexit hook can reach them later -- OTel's own
# trace.get_tracer_provider() would work for the tracer half, but there's no
# equivalent global getter for the logger half, so both are stored
# explicitly here for symmetry rather than mixing global-lookup and
# module-state approaches.
_otel_tracer_provider: TracerProvider | None = None
_otel_logger_provider: LoggerProvider | None = None


# Re-exported, NOT redefined. GovernanceDenied lives in
# parapetai_agent._exceptions so it is importable with no framework
# dependency at all -- the framework-neutral Governor facade (govern.py)
# raises the same class, and an adopter using neither MAF nor ADK must be
# able to catch it. Two class objects with the same name would look
# identical and fail every `except` that caught the other one.
#
# Semantics are unchanged and documented on the class itself: raised by a
# framework adapter's model-call hook on a Cedar deny, at whichever hook
# point that framework's enforcement is verified to be a real hard stop
# (see maf.py's "Enforcement asymmetry"). NOT raised at every hook point --
# a tool-call hook whose framework swallows exceptions uses
# result-substitution instead.


def installed_version() -> str:
    """This SDK's installed version, for the heartbeat's `version` field --
    how the control plane's fleet table reports WHICH PEP build is enforcing.

    Delegates to control_plane.sdk_version() rather than resolving a package
    name here. This function used to ask importlib.metadata for
    "parapetai-gateway", a copy-paste from the gateway's own identical helper
    where that name is correct. The gateway package is normally absent from
    an embedded SDK, so the lookup raised and every SDK PEP reported
    "0.0.0-dev"; on a host that happened to have the gateway installed it
    reported the GATEWAY's version for an SDK PEP, which is worse than
    unknown. Kept as a thin alias because maf.py and adk.py both import this
    name -- one owner, two call sites, no third copy of the bug."""
    from parapetai_agent.control_plane import sdk_version

    return sdk_version()


def bundled_default_policy_dir() -> Path:
    # Lives in parapetai-agent (parapetai-agent/src/parapetai_agent/policy/
    # default_policies/), co-located with PolicyEngine, the class that loads
    # it. Resolved relative to parapetai_agent.policy's own package file, not
    # this module's -- correct regardless of which package's wheel each ends
    # up installed under.
    return Path(parapetai_agent_policy.__file__).resolve().parent / "default_policies"


def resolve_policy_source(
    policy_dir: str | Path | None, entities_path: str | Path | None
) -> tuple[Path, Path | None]:
    """Both policy_dir/entities_path are optional -- this is what makes
    that possible without weakening default-deny. Omitting policy_dir uses
    the policy set bundled INSIDE this package (default_policies/, a real,
    read-only file installed alongside this module), so a brand new
    GovernedAgent/GovernedRunner(...) call enforces something real (base
    permits only) from the moment it's constructed, never a required setup
    step, and never requires a writable filesystem (works in a read-only
    container with no mounted volume). This is a STARTING POINT: once a
    real control-plane-provisioned agent is configured, its real bundle
    takes over.

    Omitting entities_path when policy_dir IS given looks for an
    entities.json alongside it (this repo's own convention) but doesn't
    require one to exist -- PolicyEngine already treats a missing/None
    entities_path as zero entities, never an error."""
    if policy_dir is None:
        default_dir = bundled_default_policy_dir()
        resolved_entities = (
            Path(entities_path) if entities_path is not None else default_dir / "entities.json"
        )
        return default_dir, resolved_entities
    resolved_dir = Path(policy_dir)
    if entities_path is not None:
        return resolved_dir, Path(entities_path)
    candidate = resolved_dir / "entities.json"
    return resolved_dir, candidate if candidate.exists() else None


def unresolved_alter_decision(policy_generation: int, name: str) -> Decision:
    """A synthetic deny for the one case Cedar itself never produces: an
    ALLOW whose determining policy named an @alter_with transform that
    isn't registered on this process. Fails closed per invariant 1 -- an
    unresolvable alter must never fall through to passing the original,
    unaltered content through unchanged."""
    reason = f"unregistered alter transform: {name!r}"
    return Decision(False, "deny", reason, policy_generation, 0.0, errors=(reason,))


def content_check_failure_decision(policy_generation: int, errors: tuple[str, ...]) -> Decision:
    """A synthetic deny for a configured tier-2 content check (or
    groundedness/judge check) that could not run (unknown scanner_id, or
    the scanner raised). Fails closed per invariant 1: called BEFORE
    GovernanceHook.evaluate(), so Cedar never runs and can never treat the
    resulting missing context key as "nothing to check" -- the same
    silent-bypass shape an unresolved ALTER transform would have without
    unresolved_alter_decision above."""
    reason = f"content check scanner failure: {'; '.join(errors)}"
    return Decision(False, "deny", reason, policy_generation, 0.0, errors=errors)


def oi_attribute_value(value: Any) -> str | bool | int | float | tuple[Any, ...]:
    """OTel span attribute values must be a primitive or a homogeneous
    sequence of primitives (opentelemetry.util.types.AttributeValue) -- the
    same constraint parapetai_agent.policy.hooks._otel_attribute_value
    enforces for governance attributes."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (str, bool, int, float)) for v in value
    ):
        return tuple(value)
    return json.dumps(value, default=str)


def set_oi_attributes(span: Any, attributes: dict[str, Any]) -> None:
    """Sets OpenInference attributes on `span`, skipping None values -- same
    is_recording() guard parapetai_agent.policy.hooks._set_span_attributes
    uses, so this is a genuine no-op with no tracer configured. Callers are
    responsible for only ever passing a content_bearing key when content
    logging is enabled -- this function does not itself check per-key
    sensitivity."""
    if not span.is_recording():
        return
    span.set_attributes({k: oi_attribute_value(v) for k, v in attributes.items() if v is not None})


def audit(
    decision: Decision,
    principal: str,
    snapshot: Snapshot,
    resource: str,
    context: Mapping[str, Any],
) -> None:
    """Same event name and shape as parapetai_gateway.server.app._audit's
    "decision" event for the HTTP path -- existing tooling (CLAUDE.md's dev
    loop: `jq 'select(.event=="decision")'`) works unchanged against every
    in-process framework integration too. messages_preview/response_preview/
    tool_result_preview are stripped for the same content-free-by-
    construction reason as the HTTP path -- see CLAUDE.md invariant 10;
    parapetai_agent.policy.hooks.content_free is the same strip
    GovernanceHook's own default audit callback uses.

    `principal` is taken as the caller already resolved it (via
    scoped_data.effective_principal()), not re-derived here -- so what's
    audited always matches what Cedar actually evaluated, including when
    ambient agent identity overrode a Caller's static agent_id for this
    decision.

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
    """Real OTel LogRecord for the same decision audit() just wrote as
    structlog JSON. No-op until configure_otel() has been called, so
    importing this module stays side-effect free.

    identity_claims.oid is additionally promoted to the enduser.id
    semantic-convention attribute, and identity_roles to user.roles.
    TraceId/SpanId are NOT set explicitly here: Logger.emit() with no
    explicit context pulls them from the currently active span
    automatically -- real and correct here specifically because audit() is
    always called from inside a framework adapter's own
    `with tracer.start_as_current_span(...)` block."""
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
    """Routes every structlog event from any in-process framework
    integration (and from PolicyEngine -- "policy_loaded",
    "policy_reload_rejected", etc.) to a size-bounded, rotating JSON-lines
    file under log_dir, in addition to stdout (console=True, the default).

    Not called automatically on import: importing this module must stay
    side-effect free, and this reconfigures process-wide logging, which
    only the embedding application should decide to do -- but IS idempotent
    per log_dir (constructing several GovernedAgents/GovernedRunners with
    the same local_log_dir in one process must not attach a duplicate pair
    of handlers, and therefore log every line twice, on each one). A second
    call with a DIFFERENT log_dir still attaches its own additional handler
    pair; only an identical, already-configured log_dir is skipped --
    console is only read on the FIRST call for a given log_dir, same as
    max_bytes/backup_count."""
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

    # Scoped to the "parapetai_agent" logger specifically, not root: every
    # module in this package (parapetai_agent.maf, parapetai_agent.adk,
    # parapetai_agent.policy.engine, parapetai_agent.control_plane, ...)
    # produces a "parapetai_agent.*"-named logger, which propagates up to
    # this one by Python logging's own name hierarchy. Unrelated libraries'
    # own loggers (httpx's request logging, the MCP SDK's session logging,
    # ...) are named independently and never reach this handler --
    # attaching to root instead was tried, in this module's original
    # single-package form, and pulled all of that noise into what's
    # supposed to be a narrow decision trail. propagate=False so records
    # aren't also duplicated to whatever the embedding application's own
    # root handler does.
    pkg_logger = logging.getLogger("parapetai_agent")
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
    service_name: str = "parapetai-interop",
    azure_monitor_connection_string: str | None = None,
    otlp_endpoint: str | None = None,
    otlp_headers: dict[str, str] | None = None,
    console: bool = True,
    log_mode: Literal["streaming", "buffered"] = "buffered",
    batch_max_size: int = 512,
    batch_schedule_delay_s: float = _DEFAULT_BATCH_SCHEDULE_DELAY_S,
) -> None:
    """Wires every in-process framework integration's model_call/tool_call
    decisions into real OpenTelemetry trace correlation and LogRecord
    emission.

    Sets a PROCESS-WIDE TracerProvider and LoggerProvider (OTel's own
    global-registration model, not one per Caller/PolicyEngine) --
    Resource.service.name therefore describes the process as a whole, not
    any single Cedar principal, which varies per build_middleware()/
    build_plugin() call and is carried per-record instead
    (record["principal"], already in every decision).

    console=True (default) always exports to stdout in OTel's own
    ConsoleSpanExporter/ConsoleLogExporter format via a
    SimpleSpanProcessor/SimpleLogRecordProcessor -- immediate, unbuffered,
    independent of log_mode below. azure_monitor_connection_string is
    opt-in: pass a real Application Insights connection string to also ship
    spans/logs there via azure-monitor-opentelemetry-exporter -- imported
    lazily, only when a connection string is actually supplied, so it's
    never a hard dependency of this module.

    otlp_endpoint is opt-in like azure_monitor_connection_string, and
    independent of it -- both, either, or neither may be set. Pass the
    control plane's base URL to also ship spans/logs to
    parapetai_control/otlp.py's real OTLP/HTTP (protobuf) receiver via
    genuine OTLPSpanExporter/OTLPLogExporter. otlp_headers should carry the
    agent's bearer secret since the OTLP wire format itself carries no
    agent identity.

    log_mode governs BOTH external exporters above (azure_monitor and otlp)
    uniformly:
      - "buffered" (default): SimpleSpanProcessor/SimpleLogRecordProcessor
        swapped for BatchSpanProcessor/BatchLogRecordProcessor --
        accumulates up to batch_max_size records (default 512) OR
        batch_schedule_delay_s seconds (default 120 -- TWO MINUTES,
        deliberately overriding OTel's own 5-second default) since the LAST
        export, whichever comes first, then sends one batch.
      - "streaming": every span/LogRecord exports the moment it's emitted,
        same as the console path.

    Independent of configure_rotating_audit_log(): call either, both, or
    neither. audit() feeds both sinks unconditionally; each is a no-op
    until its own configure_*() has run.

    Registers an atexit hook that flushes+shuts down both providers on
    NORMAL interpreter exit -- a long-running server should call
    flush_otel() explicitly from ITS OWN shutdown sequence instead (does
    NOT fire on SIGTERM)."""
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
    spans/LogRecords and shuts down both providers -- safe to call multiple
    times and safe to call before configure_otel() has ever run (no-op,
    nothing to flush). Registered automatically as an atexit hook by
    configure_otel() for the normal-process-exit case; call this explicitly
    from a long-running server's OWN shutdown sequence for the
    SIGTERM/graceful-shutdown case atexit alone doesn't cover."""
    if _otel_tracer_provider is not None:
        _otel_tracer_provider.shutdown()
    if _otel_logger_provider is not None:
        _otel_logger_provider.shutdown()


def otel_configured() -> bool:
    """Whether configure_otel() has already run in this process -- what
    build_middleware()/build_plugin() check before auto-wiring OTel for a
    caller who configured a control plane, so whichever framework's
    build_*() call happens first (or an embedder's own earlier
    configure_otel() call) wins, matching OTel's own set-once global
    registration."""
    return _otel_tracer_provider is not None


_tool_denials: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "parapetai_agent_governance_runtime_tool_denials", default=None
)


@contextlib.contextmanager
def track_tool_denials() -> Iterator[list[str]]:
    """Collect the reason of every tool_call a framework adapter denies
    during the wrapped block (typically one agent run). Yields the list the
    adapter appends to; read it AFTER the block to see whether any tool was
    blocked::

        with track_tool_denials() as denials:
            await agent.run(...)
        if denials:
            ...  # a tool call was governed away -- don't trust the model's text

    Exists because a denied tool_call does not always raise (some
    frameworks swallow a raised exception from a tool-call hook and convert
    it into a generic tool error result the model may or may not report
    faithfully -- see e.g. parapetai_agent.maf's module docstring's
    "Enforcement asymmetry" section for what's verified true there) -- the
    denial is only ever communicated to the MODEL as a substituted result,
    and nothing guarantees the model's own next reply faithfully reports it
    back to the end user. An embedding app that needs to know
    DETERMINISTICALLY whether any tool call was denied during a run --
    to render the turn as blocked regardless of what the model went on to
    say -- should wrap that call in this context manager and check the
    returned list afterward.

    An empty list means no tool_call was denied; a turn can still be denied
    at the model_call or content-check stage, which raise GovernanceDenied
    instead -- those are separate signals. Nesting opens an independent
    inner bucket and the outer one resumes on exit.

    A ContextVar, not an instance attribute, because a framework adapter's
    hook object is one long-lived instance shared across concurrent
    requests -- correctly isolated per asyncio task under concurrent runs."""
    bucket: list[str] = []
    token = _tool_denials.set(bucket)
    try:
        yield bucket
    finally:
        _tool_denials.reset(token)


def record_tool_denial(reason: str) -> None:
    """Append a tool_call denial reason to the active track_tool_denials()
    bucket, if one is open. A no-op when nothing is tracking (the common
    case), so a framework adapter can call this unconditionally at every
    deny without caring whether a caller is listening."""
    bucket = _tool_denials.get()
    if bucket is not None:
        bucket.append(reason)
