"""Framework-agnostic governance hook primitive.

Hoists the sequence every in-process integration needs around a single
Cedar decision -- merge context, evaluate, audit, resolve an ALTER
transform name -- out of any one framework's adapter. parapetai-agent's MAF
integration (parapetai-agent/src/parapetai_agent/maf.py) is the first, but not the
only intended, caller: a future framework adapter only ever needs to (a)
build a Snapshot from its own request/response objects -- the one
genuinely framework-specific step -- and (b) call GovernanceHook.evaluate()
at wherever that framework's own middleware/callback system fires. Nothing
here changes per framework.

Deliberately does NOT resolve a Cedar principal itself: parapetai-agent's MAF
integration overrides Caller.principal with an ambient Service Principal
identity resolved from a token (see maf.py's _effective_principal) -- that
is a MAF-specific mechanism, not a generic one, so the resolved principal
is a caller-supplied argument here, not something this module derives.

See docs/adr/0006-cedar-policy-stage-and-action-annotations.md for why
ALTER is expressed as a Cedar annotation rather than a new Decision effect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace

from parapetai_agent.identity import Caller
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.providers.parsers import Snapshot

log = structlog.get_logger(__name__)

# Keys stripped from every audited context by the default on_decision --
# CLAUDE.md invariant 10: prompt/response content never gets logged unless
# someone explicitly opts in, and this module has no opt-in knob. A caller
# that supplies its own on_decision (e.g. maf.py's _audit, which also
# fans out to OTel) is responsible for the same stripping itself.
_CONTENT_KEYS = frozenset({"messages_preview", "response_preview", "tool_result_preview"})


def content_free(context: Mapping[str, Any]) -> dict[str, Any]:
    """Drops content-bearing keys from a Cedar context dict before it's
    logged. Shared here so every on_decision implementation -- this
    module's default and any framework adapter's own -- strips the exact
    same key set rather than each hand-maintaining its own list."""
    return {k: v for k, v in context.items() if k not in _CONTENT_KEYS}


def _default_on_decision(
    decision: Decision,
    principal: str,
    snapshot: Snapshot,
    resource: str,
    context: Mapping[str, Any],
) -> None:
    record = decision.to_audit_record(
        principal=principal,
        action=snapshot.action,
        resource=resource,
        context=content_free(context),
    )
    log.info("decision", **record)


OnDecision = Callable[[Decision, str, Snapshot, str, Mapping[str, Any]], None]


def _otel_attribute_value(value: Any) -> str | bool | int | float | tuple[Any, ...]:
    """OTel span attribute values must be a primitive or a homogeneous
    sequence of primitives (opentelemetry.util.types.AttributeValue) --
    unlike a Cedar context, which allows nested dicts (scalarised
    Cedar-side by policy/engine.py's own _scalarise()). A dict, or a list
    that mixes types, gets JSON-stringified here rather than raising or
    being silently dropped by the OTel SDK."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (str, bool, int, float)) for v in value
    ):
        return tuple(value)
    return json.dumps(value, default=str)


def _set_span_attributes(
    *,
    principal: str,
    action: str,
    resource: str,
    stage: str,
    decision: Decision,
    context: Mapping[str, Any],
) -> None:
    """Sets the same data _default_on_decision (or a caller's own
    on_decision) logs onto whatever OTel span is currently active, if
    any. trace.get_current_span() is standard, framework-agnostic OTel
    API -- it picks up whatever span an adapter's own
    start_as_current_span() block left active, regardless of which
    framework that adapter is for, and is a verified no-op when no
    tracer is configured (confirmed: gateway/ has zero OTel wiring and
    stays unaffected by this). This is what makes a trace graph's
    model_call/tool_call nodes carry real parameters (tool args, model,
    identity) for ANY GovernanceHook-based framework adapter, not just
    parapetai-agent's MAF integration -- no per-framework span-attribute code
    needed anywhere else. content_free() strips the same content-bearing
    keys here as it does for the audit log, per invariant 10 -- prompt/
    response content never rides on a span either.

    Attribute keys deliberately reuse the SAME names Snapshot.to_context()
    already uses (model, provider, tool_name, tool_args, identity_claims,
    identity_roles, ...) rather than a new naming scheme, so a reader that
    falls back to a span's own attributes (e.g. control-plane's
    traces.get_trace(), for a span with no correlated decision log) can
    treat both sources identically."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    attributes: dict[str, Any] = {
        "principal": principal,
        "action": action,
        "resource": resource,
        "stage": stage,
        "decision": decision.effect,
        **content_free(context),
    }
    span.set_attributes(
        {k: _otel_attribute_value(v) for k, v in attributes.items() if v is not None}
    )


@dataclass(frozen=True, slots=True)
class HookResult:
    """decision is Cedar-native and unchanged in shape -- allowed/effect
    stay strictly binary. alter_with is populated only when the decision
    allowed AND the determining policy carried @action("alter") +
    @alter_with("<name>") -- resolved from Decision.annotations, never a
    new Cedar-level concept. None on every deny and on every plain allow;
    a caller that never inspects it behaves exactly as if ALTER did not
    exist."""

    decision: Decision
    alter_with: str | None = None


class GovernanceHook:
    """One Cedar evaluation, fully wired: context merge, evaluate, audit,
    ALTER resolution. See module docstring for what's deliberately left to
    the caller (principal resolution, applying an ALTER transform -- both
    framework-specific)."""

    def __init__(
        self,
        engine: PolicyEngine,
        caller: Caller,
        *,
        on_decision: OnDecision | None = None,
    ) -> None:
        self.engine = engine
        self.caller = caller
        self._on_decision = on_decision or _default_on_decision

    def evaluate(
        self,
        *,
        snapshot: Snapshot,
        stage: str,
        principal: str | None = None,
        extra_context: Mapping[str, Any] | None = None,
    ) -> HookResult:
        """stage is "pre" or "post" -- see PolicyEngine.evaluate()'s module
        docstring for what each filters to. principal defaults to
        self.caller.principal; pass the caller's own resolved value (e.g.
        an ambient Service Principal identity) to override it for this one
        decision without mutating the Caller the hook was constructed
        with.

        extra_context, when given, is merged OVER full_context_for()'s own
        keys (so it can override, though no current caller relies on
        that) before evaluate() ever sees it. Exists for a caller that
        computes something Snapshot/Caller alone can't -- parapetai_agent's
        tier-2 content-check scanners are the first: they run against
        snapshot.messages_preview BEFORE this call and hand back FLAT,
        top-level keys (e.g. {"content_checks_pii_types": [...]}) for a
        generated Cedar rule to gate on -- flat deliberately, not nested
        under a wrapper key, since PolicyEngine._scalarise() only keeps a
        list as a real Cedar Set (rather than JSON-stringifying it) at the
        top level of context. Deliberately NOT stripped by content_free() below:
        that strips raw prompt/response TEXT (invariant 10), and a
        content-check result is derived metadata (which entity types or
        categories matched), never the text itself -- the same category
        as tool_name or identity_roles, not messages_preview. A caller
        whose extra_context WOULD carry raw text must not rely on this
        function to redact it; nothing here inspects the values."""
        resolved_principal = principal or self.caller.principal
        resource = f'Resource::"{snapshot.provider}"'
        full_context = full_context_for(snapshot, self.caller)
        if extra_context:
            full_context = {**full_context, **extra_context}
        decision = self.engine.evaluate(
            principal=resolved_principal,
            action=snapshot.action,
            resource=resource,
            context=full_context,
            stage=stage,
        )
        self._on_decision(decision, resolved_principal, snapshot, resource, full_context)
        _set_span_attributes(
            principal=resolved_principal,
            action=snapshot.action,
            resource=resource,
            stage=stage,
            decision=decision,
            context=full_context,
        )
        alter_with = decision.annotations.get("alter_with") if decision.allowed else None
        return HookResult(decision=decision, alter_with=alter_with)


def full_context_for(snapshot: Snapshot, caller: Caller) -> dict[str, Any]:
    """Snapshot.to_context() alone is not the full Cedar context: tenant/
    trust_tier live on the Caller (identity), not the parsed
    request/response. Same merge every in-process and HTTP call site needs
    -- hoisted here so a future framework adapter gets it for free instead
    of re-deriving it (a real, previously-shipped bug in parapetai-agent's MAF
    integration was exactly this merge being missed once)."""
    return {**snapshot.to_context(), "tenant": caller.tenant, "trust_tier": caller.trust_tier}
