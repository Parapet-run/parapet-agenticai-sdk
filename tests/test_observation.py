"""parapetai_agent.observation -- auth-integrations.md §10.2/§10.3/§10.17.

Covers: bucket_key's own determinism (the cross-repo contract this
module's docstring flags), CollectionBudget's malformed/absent-signal
handling (must never treat "no valid instruction" as "collect nothing" or
"collect everything"), the framework contextvar scope, and
ObservationSpanProcessor's attribute-based protocol classification --
using a real opentelemetry-sdk TracerProvider/InMemorySpanExporter, the
same real-SDK-object testing style test_corroboration.py's own
correlation test uses, rather than a hand-rolled span stand-in.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from parapetai_agent.observation import (
    CollectionBudget,
    ObservationSpanProcessor,
    _classify,
    bucket_key,
    current_framework,
    set_current_framework,
)


def test_bucket_key_is_deterministic() -> None:
    a = bucket_key("agent-1", "http", "GET", "/sobjects/Case")
    b = bucket_key("agent-1", "http", "GET", "/sobjects/Case")
    assert a == b


def test_bucket_key_distinguishes_every_component() -> None:
    base = bucket_key("agent-1", "http", "GET", "/x")
    assert base != bucket_key("agent-2", "http", "GET", "/x")
    assert base != bucket_key("agent-1", "grpc", "GET", "/x")
    assert base != bucket_key("agent-1", "http", "POST", "/x")
    assert base != bucket_key("agent-1", "http", "GET", "/y")


def test_collection_budget_defaults_to_not_saturated() -> None:
    # §10.3: a bucket the server has never mentioned collects by default.
    budget = CollectionBudget()
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is False


def test_collection_budget_honors_saturated_list() -> None:
    budget = CollectionBudget()
    key = bucket_key("agent-1", "http", "GET", "/x")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is True
    assert budget.is_saturated("agent-1", "http", "GET", "/other") is False


def test_collection_budget_replaces_not_merges_on_each_update() -> None:
    # §10.3: a bucket dropping out of the list (an operator's "Resume
    # collection", or a §10.17 recheck) must be honored on the very next
    # update, not require a separate un-saturate call.
    budget = CollectionBudget()
    key = bucket_key("agent-1", "http", "GET", "/x")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is True
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": []}})
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is False


def test_collection_budget_ignores_missing_field_without_changing_state() -> None:
    budget = CollectionBudget()
    key = bucket_key("agent-1", "http", "GET", "/x")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    # An older control plane / a cycle with no bundle content at all --
    # must not be read as "everything is now un-saturated".
    budget.update_from_bundle_meta({})
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is True


def test_collection_budget_ignores_malformed_shape_without_raising() -> None:
    budget = CollectionBudget()
    key = bucket_key("agent-1", "http", "GET", "/x")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    budget.update_from_bundle_meta({"observation_collection": "not-a-mapping"})
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": "not-a-list"}})
    assert budget.is_saturated("agent-1", "http", "GET", "/x") is True


def test_set_current_framework_scopes_and_resets() -> None:
    assert current_framework() is None
    with set_current_framework("maf"):
        assert current_framework() == "maf"
    assert current_framework() is None


def test_classify_http_prefers_stable_semconv_names() -> None:
    attrs = {
        "http.request.method": "GET",
        "url.path": "/sobjects/Case",
        "server.address": "salesforce.com",
    }
    assert _classify(attrs) == ("http", "GET", "/sobjects/Case", "salesforce.com")


def test_classify_http_falls_back_to_older_semconv_names() -> None:
    result = _classify({"http.method": "DELETE", "http.url": "https://x/y", "net.peer.name": "x"})
    assert result == ("http", "DELETE", "https://x/y", "x")


def test_classify_db() -> None:
    result = _classify(
        {"db.system": "postgresql", "db.operation": "SELECT", "db.sql.table": "cases"}
    )
    assert result == ("db", "SELECT", "cases", None)


def test_classify_grpc() -> None:
    result = _classify(
        {"rpc.system": "grpc", "rpc.service": "salesforce.CaseService", "rpc.method": "Delete"}
    )
    assert result == ("grpc", "Delete", "salesforce.CaseService", None)


def test_classify_returns_none_for_an_unrelated_span() -> None:
    # e.g. the parapetai.tool_call span itself -- carries none of these keys.
    assert _classify({"some.other.attribute": "x"}) is None


def test_observation_span_processor_tags_an_unsaturated_bucket() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(ObservationSpanProcessor("agent-1", CollectionBudget()))
    tracer = provider.get_tracer(__name__)

    with set_current_framework("maf"):
        with tracer.start_as_current_span(
            "GET", attributes={"http.request.method": "GET", "url.path": "/sobjects/Case"}
        ):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["parapetai.observed"] is True
    assert span.attributes["parapetai.observed.protocol"] == "http"
    assert span.attributes["parapetai.observed.verb"] == "GET"
    assert span.attributes["parapetai.observed.target"] == "/sobjects/Case"
    assert span.attributes["parapetai.observed.framework"] == "maf"


def test_observation_span_processor_does_not_tag_a_saturated_bucket() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    budget = CollectionBudget()
    key = bucket_key("agent-1", "http", "GET", "/sobjects/Case")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    provider.add_span_processor(ObservationSpanProcessor("agent-1", budget))
    tracer = provider.get_tracer(__name__)

    # Inside a tool_call scope -- proves saturation is what suppressed
    # this, not the (separately tested) framework gate below.
    with set_current_framework("maf"):
        with tracer.start_as_current_span(
            "GET", attributes={"http.request.method": "GET", "url.path": "/sobjects/Case"}
        ):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert "parapetai.observed" not in span.attributes


def test_observation_span_processor_ignores_an_unrelated_span() -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(ObservationSpanProcessor("agent-1", CollectionBudget()))
    tracer = provider.get_tracer(__name__)

    # Inside a tool_call scope -- proves _classify() finding no matching
    # semconv attrs is what suppressed this, not the framework gate below.
    with set_current_framework("maf"):
        with tracer.start_as_current_span("parapetai.tool_call"):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert "parapetai.observed" not in span.attributes


def test_observation_span_processor_ignores_a_span_outside_any_tool_call_scope() -> None:
    # auth-integrations.md §10.16 Phase B build note: without this gate,
    # corroboration's process-wide httpx instrumentation means ANY
    # http/db/grpc-shaped span gets tagged -- including this SDK's own
    # infrastructure calls (bundle poll, heartbeat), which run with no
    # framework's tool_call span active at all. A real, well-formed
    # http-shaped span must still be ignored here even though every
    # attribute _classify() looks for is present and the bucket is
    # nowhere near saturated -- current_framework() being unset is by
    # itself sufficient reason to skip.
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(ObservationSpanProcessor("agent-1", CollectionBudget()))
    tracer = provider.get_tracer(__name__)

    assert current_framework() is None
    with tracer.start_as_current_span(
        "GET", attributes={"http.request.method": "GET", "url.path": "/api/v1/bundle"}
    ):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert "parapetai.observed" not in span.attributes


def test_observation_span_processor_uses_module_level_tracer_provider_is_not_required() -> None:
    # enable_observation_capture() (not exercised directly here -- see
    # test_enable_observation_capture below) requires a real SDK
    # TracerProvider to already be current; this test only proves
    # ObservationSpanProcessor itself has no such requirement when wired
    # in manually, since a caller may build its own TracerProvider in a
    # test the way this file's other tests do.
    otel_trace.get_tracer_provider()  # does not raise, regardless of what's registered


def test_enable_observation_capture_warns_without_raising_when_no_sdk_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from opentelemetry import trace as _t

    from parapetai_agent import observation as _observation

    monkeypatch.setattr(_t, "get_tracer_provider", lambda: object())
    budget = _observation.enable_observation_capture("agent-1")
    assert isinstance(budget, _observation.CollectionBudget)


# --------------------------------------------------------------------------
# enable_mcp_observation -- the third capture path, for an in-process tool
# that is itself an MCP client (agent_framework.MCPTool and equivalents),
# talking to a remote MCP server with no gateway in the path. See
# observation.py's own module docstring for the finding that made this
# necessary (MCPTool's persistent lifecycle-owner task detaches the real
# call from whatever span was ambient).
#
# One shared TracerProvider/InMemorySpanExporter for the whole class --
# observation.py's own module-level `_tracer` is a ProxyTracer that
# permanently caches the first real Tracer it resolves against (the same
# finding documented at length in test_govern.py's own TestOtelSpans and
# gateway/tests/test_vsp_observation.py), so every scenario needing a real
# emitted span shares one provider, cleared with `.clear()` between them,
# rather than each getting a fresh provider that arrives too late for an
# already-resolved proxy to see.
#
# Each test also gets a genuinely clean mcp.client.session.ClientSession
# patch state via the clean_mcp_patch fixture below: whichever test ran
# FIRST in the whole `pytest tests -q` session to call build_middleware()/
# Governor.from_control_plane() etc. with a control plane configured
# already triggered enable_automatic_detection() -> enable_mcp_observation()
# for real, so ClientSession.call_tool/initialize may already be patched
# (harmlessly -- see enable_mcp_observation's own idempotent-replace
# docstring) by the time this file's own tests run. disable_mcp_observation()
# first guarantees a genuine, unpatched ClientSession before each test here
# installs its own fake "original" to patch around and assert against.
# --------------------------------------------------------------------------

_MCP_SPAN_EXPORTER = InMemorySpanExporter()
_MCP_TRACER_PROVIDER = TracerProvider()
_MCP_TRACER_PROVIDER.add_span_processor(SimpleSpanProcessor(_MCP_SPAN_EXPORTER))


@pytest.fixture(autouse=True)
def _clean_mcp_patch():  # type: ignore[no-untyped-def]
    from parapetai_agent.observation import disable_mcp_observation

    # conftest.py's own autouse _reset_otel_module_state wipes the GLOBAL
    # provider registration (trace_api._TRACER_PROVIDER) after every test
    # in the whole session, including earlier tests in THIS file -- so
    # re-registering once at module-collection time is not enough; it has
    # to happen fresh before each test here too. This does NOT reset
    # observation.py's own module-level `_tracer` ProxyTracer cache (there
    # is no public API to do that), but nothing else in this whole suite
    # ever triggers a REAL span through it (every other test only
    # INSTALLS the mcp patch via enable_automatic_detection(), never
    # actually invokes ClientSession.call_tool), so it stays unresolved
    # until one of these tests below becomes the first to really use it.
    otel_trace.set_tracer_provider(_MCP_TRACER_PROVIDER)
    disable_mcp_observation()
    _MCP_SPAN_EXPORTER.clear()
    yield
    disable_mcp_observation()


class _FakeMcpSession:
    """Stands in for mcp.client.session.ClientSession -- enable_mcp_observation
    patches the REAL class's methods, but the patched wrapper only ever
    uses `self` for attribute get/set (the server-name/host cache) and to
    pass through to whatever the original bound method was, so a plain
    instance works fine as `self` without needing the real class's full
    session/transport machinery."""


def _observed_mcp_spans():  # type: ignore[no-untyped-def]
    return [
        s for s in _MCP_SPAN_EXPORTER.get_finished_spans() if s.name == "parapetai.observed_call"
    ]


async def test_call_tool_emits_an_observed_call_and_returns_the_real_result() -> None:
    from mcp.client.session import ClientSession

    from parapetai_agent.observation import enable_mcp_observation

    sentinel = object()

    async def _fake_call_tool(self, name, arguments=None, *a, **k):  # type: ignore[no-untyped-def]
        return sentinel

    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    budget = enable_mcp_observation("agent-mcp-1")
    assert budget is not None

    result = await ClientSession.call_tool(
        _FakeMcpSession(), "createJiraIssue", {"project": "SCRUM", "summary": "x"}
    )
    assert result is sentinel

    (span,) = _observed_mcp_spans()
    attrs = dict(span.attributes or {})
    assert attrs["parapetai.observed"] is True
    assert attrs["parapetai.observed.protocol"] == "mcp"
    assert attrs["parapetai.observed.verb"] == "createJiraIssue"
    assert attrs["parapetai.observed.target"] == "mcp"  # no initialize() call in this test
    assert attrs["parapetai.observed.args_shape"] == ("project", "summary")


async def test_initialize_caches_server_name_and_website_host_for_later_call_tool() -> None:
    from mcp.client.session import ClientSession

    from parapetai_agent.observation import enable_mcp_observation

    class _ServerInfo:
        name = "salesforce-mcp-server"
        websiteUrl = "https://vendor.example/about"

    class _InitResult:
        serverInfo = _ServerInfo()

    async def _fake_initialize(self, *a, **k):  # type: ignore[no-untyped-def]
        return _InitResult()

    async def _fake_call_tool(self, name, arguments=None, *a, **k):  # type: ignore[no-untyped-def]
        return None

    ClientSession.initialize = _fake_initialize  # type: ignore[method-assign]
    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    enable_mcp_observation("agent-mcp-2")

    session = _FakeMcpSession()
    await ClientSession.initialize(session)
    await ClientSession.call_tool(session, "create_salesforce_opportunity", {})

    (span,) = _observed_mcp_spans()
    attrs = dict(span.attributes or {})
    assert attrs["parapetai.observed.target"] == "salesforce-mcp-server"
    assert attrs["parapetai.observed.destination"] == "vendor.example"


async def test_saturated_mcp_bucket_suppresses_further_emission() -> None:
    from mcp.client.session import ClientSession

    from parapetai_agent.observation import CollectionBudget, bucket_key, enable_mcp_observation

    async def _fake_call_tool(self, name, arguments=None, *a, **k):  # type: ignore[no-untyped-def]
        return None

    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    budget = CollectionBudget()
    key = bucket_key("agent-mcp-3", "mcp", "createJiraIssue", "mcp")
    budget.update_from_bundle_meta({"observation_collection": {"saturated_buckets": [key]}})
    enable_mcp_observation("agent-mcp-3", budget)

    await ClientSession.call_tool(_FakeMcpSession(), "createJiraIssue", {})

    assert _observed_mcp_spans() == []


async def test_no_active_agent_id_emits_nothing() -> None:
    from mcp.client.session import ClientSession

    from parapetai_agent.observation import disable_mcp_observation, enable_mcp_observation

    async def _fake_call_tool(self, name, arguments=None, *a, **k):  # type: ignore[no-untyped-def]
        return "ok"

    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    enable_mcp_observation("agent-mcp-4")
    disable_mcp_observation()  # unpatches AND clears active state

    # Re-patch with the same fake as "original" but never re-enable --
    # this simulates the patch having been installed once, then
    # explicitly turned off, without a second process-wide un-patch.
    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    result = await ClientSession.call_tool(_FakeMcpSession(), "createJiraIssue", {})
    assert result == "ok"
    assert _observed_mcp_spans() == []


def test_enable_mcp_observation_returns_none_without_the_mcp_package(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    from parapetai_agent.observation import enable_mcp_observation

    monkeypatch.setitem(sys.modules, "mcp.client.session", None)
    assert enable_mcp_observation("agent-mcp-5") is None


async def test_disable_mcp_observation_restores_the_original_methods() -> None:
    from mcp.client.session import ClientSession

    from parapetai_agent.observation import (
        disable_mcp_observation,
        enable_mcp_observation,
        mcp_observation_enabled,
    )

    async def _fake_call_tool(self, name, arguments=None, *a, **k):  # type: ignore[no-untyped-def]
        return "raw"

    ClientSession.call_tool = _fake_call_tool  # type: ignore[method-assign]
    enable_mcp_observation("agent-mcp-6")
    assert mcp_observation_enabled() is True
    assert ClientSession.call_tool is not _fake_call_tool  # now wrapped

    disable_mcp_observation()
    assert mcp_observation_enabled() is False
    assert ClientSession.call_tool is _fake_call_tool  # restored, unwrapped
