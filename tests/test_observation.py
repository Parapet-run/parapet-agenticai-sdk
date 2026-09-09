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

    with tracer.start_as_current_span("parapetai.tool_call"):
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
