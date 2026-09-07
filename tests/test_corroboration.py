"""parapetai_agent.corroboration -- Tier 1 auto-instrumentation
(auth-integrations.md §7). Proves the two properties that actually
matter: (1) a real network call a tool makes during its own execution
emits a span correctly correlated (parent_span_id) to that tool's own
parapetai.tool_call span, through arbitrary vendor-SDK wrapping depth
included, and (2) an instrumentor whose target isn't installed at all is
reported as not-activated, never raises.

The correlation proof (TestRealCorrelation below) runs in a SEPARATE
subprocess, not in-process like the rest of this suite -- confirmed live
that enabling real httpx instrumentation in-process, even with
disable_http_corroboration() called afterward, leaves the shared pytest
process in a state where a LATER, otherwise-passing test in a different
file (test_maf.py's own OTelCorrelation test, which makes its own real
httpx call to a local fake-upstream server) stops recording spans --
tracked as a real, open question about interaction between wrapt's
monkeypatching and this suite's OTel TracerProvider-reset fixture
(tests/conftest.py's autouse _reset_otel_module_state), not fully root
caused. Subprocess isolation sidesteps it entirely and is the same
technique test_maf.py's own `fake_upstream`/`mcp_server` fixtures already
use for "needs a genuinely clean environment" cases -- proven correct
either way, just without risking every other test in the shared process.

Does NOT test OTel's own dependency-conflict detection (BaseInstrumentor
itself, verified interactively against the real opentelemetry-instrumentation
package before writing this) -- only this module's own wrapper logic
around it.
"""

from __future__ import annotations

import subprocess
import sys

from parapetai_agent.corroboration import _try_instrument, enable_http_corroboration


class _FakeCandidate:
    """Duck-typed stand-in for corroboration._Candidate, naming a module
    that genuinely does not exist -- proves the ImportError branch
    (the instrumentor PACKAGE itself missing) returns False rather than
    propagating."""

    module = "opentelemetry.instrumentation.this_transport_does_not_exist"
    class_name = "NoSuchInstrumentor"
    library = "nonexistent"


def test_try_instrument_returns_false_for_an_uninstalled_instrumentor_package() -> None:
    assert _try_instrument(_FakeCandidate()) is False  # type: ignore[arg-type]


def test_enable_reports_per_library_status_and_never_raises() -> None:
    result = enable_http_corroboration()
    assert set(result) == {"httpx", "requests", "urllib3", "aiohttp", "grpc"}
    assert all(isinstance(v, bool) for v in result.values())


def test_enable_is_idempotent() -> None:
    first = enable_http_corroboration()
    second = enable_http_corroboration()
    assert first == second


_CORRELATION_SCRIPT = """
import asyncio
import respx
from httpx import Response
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from parapetai_agent.corroboration import enable_http_corroboration
from parapetai_agent.identity import Caller
from parapetai_agent.maf import ParapetFunctionMiddleware
from parapetai_agent.policy.engine import PolicyEngine

import tempfile
from pathlib import Path


def _permit_all_policy_dir():
    d = Path(tempfile.mkdtemp())
    (d / "00-base.cedar").write_text(
        'permit (principal, action == Action::"model_call", resource);\\n'
        'permit (principal, action == Action::"tool_call", resource);\\n'
    )
    return d


@respx.mock
async def main():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)

    enable_http_corroboration()
    respx.get("https://vendor.example/api/case/500x").mock(
        return_value=Response(200, json={"status": "deleted"})
    )

    import httpx

    def delete_salesforce_case(case_id: str) -> str:
        # A "vendor SDK" stand-in -- whatever wraps this real httpx call
        # (however many layers deep in a real integration), THIS is the
        # point where the call actually leaves the process, exactly what
        # Tier 1 patches, with no awareness of what called it.
        resp = httpx.get(f"https://vendor.example/api/case/{case_id}")
        return str(resp.json()["status"])

    engine = PolicyEngine(_permit_all_policy_dir())
    caller = Caller(agent_id="corroboration-test", tenant="default")
    mw = ParapetFunctionMiddleware(engine, caller)

    from agent_framework import FunctionInvocationContext, FunctionTool

    fn = FunctionTool(name="delete_salesforce_case", func=delete_salesforce_case)
    ctx = FunctionInvocationContext(function=fn, arguments={"case_id": "500x"})

    async def call_next():
        ctx.result = delete_salesforce_case("500x")

    await mw.process(ctx, call_next)
    assert ctx.result == "deleted"

    spans = exporter.get_finished_spans()
    tool_span = next(s for s in spans if s.name == "parapetai.tool_call")
    http_span = next(s for s in spans if s.name == "GET")
    assert http_span.parent is not None
    assert http_span.parent.span_id == tool_span.context.span_id
    assert http_span.parent.trace_id == tool_span.context.trace_id
    print("CORRELATION_OK")


asyncio.run(main())
"""


def test_a_real_httpx_call_inside_a_tool_is_a_child_of_its_tool_call_span() -> None:
    """Subprocess-isolated -- see this module's own docstring for why."""
    result = subprocess.run(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [sys.executable, "-c", _CORRELATION_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "CORRELATION_OK" in result.stdout, (
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
