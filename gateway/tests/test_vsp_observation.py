"""auth-integrations.md §10.16 Phase B: the gateway's own capture path for
automatic VendorScopePermission detection on the MCP path. MCPParser already
extracts tool_name/tool_args off the wire with certainty (no ambient
network-library span to piggyback on, unlike the in-process SDK's
corroboration-based capture) -- server/app.py's _observe_mcp_call constructs
an ObservedCall directly and exports it as a standalone
"parapetai.observed_call" span via parapetai_agent.observation.
emit_observed_span.

One shared TracerProvider/InMemorySpanExporter for the whole module, not one
per test -- see tests/test_govern.py's own TestOtelSpans class in the SDK
repo for why: opentelemetry.trace.ProxyTracer (what
`trace.get_tracer(__name__)` returns, and what server/app.py's module-level
`_tracer` is) permanently caches the first real Tracer it resolves against,
so a second `set_tracer_provider()` call later in the same process is
invisible to an already-resolved ProxyTracer. This module is the only place
in the gateway's whole test suite that ever triggers a real span through
that module-level `_tracer` (every other gateway test leaves
PARAPETAI_AGENT_ID unset, which _observe_mcp_call's own guard treats as
"nothing to key a bucket on, skip"), so setting the provider once at import
time is both correct and sufficient -- no earlier test can have already
locked the proxy to some other provider.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import parapetai_gateway.server.app as app_module
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"

_SPAN_EXPORTER = InMemorySpanExporter()
_TRACER_PROVIDER = TracerProvider()
_TRACER_PROVIDER.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
otel_trace.set_tracer_provider(_TRACER_PROVIDER)

_TOOL_CALL = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "lookup_order", "arguments": {"order_id": "A1001"}},
}


@pytest.fixture(autouse=True)
def _clear_spans() -> None:
    _SPAN_EXPORTER.clear()


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> TestClient:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, **overrides)
    )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine))


def _observed_spans() -> list[object]:
    return [s for s in _SPAN_EXPORTER.get_finished_spans() if s.name == "parapetai.observed_call"]


@respx.mock
def test_allowed_tool_call_emits_an_observed_span(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://jira-mcp.internal:9000/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    client = _client(
        monkeypatch,
        agent_id="gw-fleet-1",
        mcp_upstreams={"jira": "https://jira-mcp.internal:9000/mcp"},
    )

    resp = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    assert resp.status_code == 200

    (span,) = _observed_spans()
    attrs = dict(span.attributes or {})
    assert attrs["parapetai.observed"] is True
    assert attrs["parapetai.observed.protocol"] == "mcp"
    assert attrs["parapetai.observed.verb"] == "lookup_order"
    assert attrs["parapetai.observed.target"] == "jira"
    assert attrs["parapetai.observed.destination"] == "jira-mcp.internal"
    assert attrs["parapetai.observed.args_shape"] == ("order_id",)


@respx.mock
def test_bare_mcp_path_uses_mcp_as_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://default.example").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    monkeypatch.setenv("PARAPETAI_MCP_BASE_URL", "https://default.example")
    client = _client(monkeypatch, agent_id="gw-fleet-1")

    resp = client.post("/a/probe/mcp", json=_TOOL_CALL)
    assert resp.status_code == 200

    (span,) = _observed_spans()
    assert dict(span.attributes or {})["parapetai.observed.target"] == "mcp"


@respx.mock
def test_no_agent_id_configured_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default in every other gateway test: no PARAPETAI_AGENT_ID means
    # there is no OTLP-authenticated identity a bucket key could ever match
    # server-side (server/app.py's _observe_mcp_call docstring) -- skip
    # entirely rather than emit under some other identity. Upstream IS
    # configured and the call DOES succeed here, isolating this from the
    # separate no-upstream-configured case.
    respx.post("https://jira-mcp.internal:9000/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    client = _client(
        monkeypatch,
        agent_id=None,
        mcp_upstreams={"jira": "https://jira-mcp.internal:9000/mcp"},
    )

    resp = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    assert resp.status_code == 200

    assert _observed_spans() == []


@respx.mock
def test_observation_capture_false_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same escape hatch as the in-process SDK's own
    # governance_runtime.resolve_observation_capture_enabled() --
    # PARAPETAI_OBSERVATION_CAPTURE=false disables this fleet-wide
    # regardless of which PEP a deployment uses.
    respx.post("https://jira-mcp.internal:9000/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    client = _client(
        monkeypatch,
        agent_id="gw-fleet-1",
        mcp_upstreams={"jira": "https://jira-mcp.internal:9000/mcp"},
        observation_capture=False,
    )

    resp = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    assert resp.status_code == 200

    assert _observed_spans() == []


@respx.mock
def test_denied_and_enforced_call_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # execute_shell is denied by the fixture policy set -- an enforced deny
    # never reaches upstream, so it must never be observed either: the same
    # "only a call that actually happens gets captured" principle the
    # in-process SDK's own corroboration-piggybacked spans already have (a
    # blocked tool call never runs, so never makes a real network call to
    # tag). No respx route registered -- a forward attempt would error the
    # test, proving this really is blocked before _forward().
    client = _client(monkeypatch, agent_id="gw-fleet-1", mode="enforce")

    resp = client.post(
        "/a/probe/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "execute_shell", "arguments": {"command": "rm -rf /"}},
        },
    )
    assert resp.status_code == 403
    assert _observed_spans() == []


@respx.mock
def test_non_tool_call_mcp_method_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://default.example").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    monkeypatch.setenv("PARAPETAI_MCP_BASE_URL", "https://default.example")
    client = _client(monkeypatch, agent_id="gw-fleet-1")

    resp = client.post(
        "/a/probe/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "params": {}}
    )
    assert resp.status_code == 200
    assert _observed_spans() == []


@respx.mock
def test_saturated_bucket_suppresses_further_emission(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post("https://jira-mcp.internal:9000/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    client = _client(
        monkeypatch,
        agent_id="gw-fleet-1",
        mcp_upstreams={"jira": "https://jira-mcp.internal:9000/mcp"},
    )

    client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    assert len(_observed_spans()) == 1

    # Same shape as the bundle-poll response's own field
    # (observation.CollectionBudget.update_from_bundle_meta) -- server/
    # main.py wires this as run_bundle_poller's on_bundle_meta callback in
    # production; the test drives it directly, same as
    # test_observation.py does for the in-process SDK side.
    from parapetai_agent.observation import bucket_key

    key = bucket_key("gw-fleet-1", "mcp", "lookup_order", "jira")
    app = client.app
    app.state.vsp_budget.update_from_bundle_meta(
        {"observation_collection": {"saturated_buckets": [key]}}
    )
    _SPAN_EXPORTER.clear()

    client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    assert _observed_spans() == []
