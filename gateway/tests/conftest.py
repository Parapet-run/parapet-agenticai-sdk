"""One process-wide TracerProvider for the whole gateway suite.

OpenTelemetry allows exactly one global provider per process, and
`trace.get_tracer(__name__)` (what server/app.py's module-level `_tracer` is)
permanently caches the first real Tracer it resolves. Now that every proxied
request opens a decision span, any test module could be first to resolve it --
so the provider is installed here, in conftest (imported before any test
module), rather than by whichever module happens to need spans.
"""

from __future__ import annotations

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SPAN_EXPORTER = InMemorySpanExporter()
_TRACER_PROVIDER = TracerProvider()
_TRACER_PROVIDER.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
otel_trace.set_tracer_provider(_TRACER_PROVIDER)
