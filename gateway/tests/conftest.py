"""One process-wide TracerProvider for the whole gateway suite.

OpenTelemetry allows exactly one global provider per process, and
`trace.get_tracer(__name__)` (what server/app.py's module-level `_tracer` is)
permanently caches the first real Tracer it resolves. Now that every proxied
request opens a decision span, any test module could be first to resolve it --
so the provider is installed here, in conftest (imported before any test
module), rather than by whichever module happens to need spans.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPAN_EXPORTER = InMemorySpanExporter()
_TRACER_PROVIDER = TracerProvider()
_TRACER_PROVIDER.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
otel_trace.set_tracer_provider(_TRACER_PROVIDER)

# Imported AFTER the provider above is installed, so the module-level tracer in
# server/app.py can only ever resolve against it.
import parapetai_gateway.server.app as app_module  # noqa: E402


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records what _audit hands governance_runtime.audit, plus whether a span
    was active at that moment -- the condition for the audit LogRecord to
    carry a trace id."""
    seen: list[dict[str, Any]] = []
    real = app_module.governance_runtime.audit

    def recorder(decision: Any, *, principal: str, snapshot: Any, resource: str, context: Any):
        seen.append(
            {
                "principal": principal,
                "resource": resource,
                "context": dict(context),
                "span_active": otel_trace.get_current_span().get_span_context().is_valid,
            }
        )
        real(decision, principal=principal, snapshot=snapshot, resource=resource, context=context)

    monkeypatch.setattr(app_module.governance_runtime, "audit", recorder)
    return seen
