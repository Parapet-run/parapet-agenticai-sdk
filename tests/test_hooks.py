"""parapetai_agent.policy.hooks.GovernanceHook -- the framework-agnostic
pre/post evaluation primitive. Verifies the context merge (tenant/
trust_tier), principal override, ALTER resolution via Decision.annotations,
the audit callback firing exactly once with content-free context by
default, and that a pluggable on_decision replaces the default entirely.
"""

from __future__ import annotations

from pathlib import Path

from parapetai_agent.identity import Caller
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook, content_free, full_context_for
from parapetai_agent.providers.parsers import Snapshot


def _write(policy_dir: Path, name: str, text: str) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / name).write_text(text)


def _engine(tmp_path: Path) -> PolicyEngine:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-alter.cedar",
        '@id("alter_rule")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
        'permit(principal, action == Action::"model_call", resource)\n'
        'when { context has response_preview && context.response_preview like "*secret*" };',
    )
    return PolicyEngine(tmp_path)


def test_full_context_merges_tenant_and_trust_tier() -> None:
    caller = Caller(agent_id="a1", tenant="acme", trust_tier="untrusted")
    snapshot = Snapshot(provider="openai", endpoint="in-process:maf:model_call", parsed=True)
    ctx = full_context_for(snapshot, caller)
    assert ctx["tenant"] == "acme"
    assert ctx["trust_tier"] == "untrusted"
    assert ctx["provider"] == "openai"


def test_content_free_strips_preview_fields_only() -> None:
    ctx = {
        "provider": "openai",
        "messages_preview": "hello",
        "response_preview": "world",
        "tool_result_preview": "secret",
    }
    stripped = content_free(ctx)
    assert stripped == {"provider": "openai"}


def test_evaluate_defaults_principal_to_caller(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(provider="openai", endpoint="in-process:maf:model_call", parsed=True)

    result = hook.evaluate(snapshot=snapshot, stage="pre")
    assert result.decision.allowed
    assert result.alter_with is None


def test_evaluate_principal_override_takes_precedence(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    caller = Caller(agent_id="static-id", tenant="default")
    seen: list[str] = []

    def on_decision(
        decision: Decision, principal: str, snapshot: Snapshot, resource: str, context: dict
    ) -> None:  # noqa: E501
        seen.append(principal)

    hook = GovernanceHook(engine, caller, on_decision=on_decision)
    snapshot = Snapshot(provider="openai", endpoint="in-process:maf:model_call", parsed=True)

    hook.evaluate(snapshot=snapshot, stage="pre", principal='Agent::"overridden"')
    assert seen == ['Agent::"overridden"']


def test_alter_with_resolved_from_annotations_on_allow(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:maf:model_call",
        parsed=True,
        response_preview="the secret is out",
    )

    result = hook.evaluate(snapshot=snapshot, stage="post")
    assert result.decision.allowed
    assert result.alter_with == "redact_all"


def test_alter_with_none_when_content_does_not_match(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:maf:model_call",
        parsed=True,
        response_preview="nothing interesting here",
    )

    result = hook.evaluate(snapshot=snapshot, stage="post")
    assert result.decision.allowed
    assert result.alter_with is None


def test_alter_with_none_at_pre_stage_even_if_content_would_match(tmp_path: Path) -> None:
    """The alter rule in _engine() is @stage("post")-only -- at "pre" it's
    filtered out entirely, so even matching content resolves no alter_with.
    This is the whole enforcement boundary for "ALTER is post-call only":
    the caller (maf.py's pre-call hook) never even sees a non-None
    alter_with to (wrongly) act on."""
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:maf:model_call",
        parsed=True,
        response_preview="the secret is out",
    )

    result = hook.evaluate(snapshot=snapshot, stage="pre")
    assert result.decision.allowed
    assert result.alter_with is None


def test_default_on_decision_logs_content_free(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:maf:model_call",
        parsed=True,
        response_preview="the secret is out",
    )

    with capture_logs() as events:
        hook.evaluate(snapshot=snapshot, stage="post")

    decision_events = [e for e in events if e.get("event") == "decision"]
    assert len(decision_events) == 1
    assert "response_preview" not in decision_events[0]["context"]


def _permit_all_engine(tmp_path: Path) -> PolicyEngine:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n',
    )
    return PolicyEngine(tmp_path)


def test_span_attributes_set_when_a_tracer_is_recording(tmp_path: Path) -> None:
    """Framework-agnostic: GovernanceHook.evaluate() sets attributes on
    whatever span is ambiently current, using the SAME key names
    Snapshot.to_context() uses (so a reader that falls back to a span's
    own attributes, e.g. control-plane's trace view for a span with no
    correlated decision log, can treat both sources identically)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    engine = _permit_all_engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="acme")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:test:tool_call",
        parsed=True,
        tool_name="lookup_order",
        tool_args={"order_id": "123"},
    )

    with tracer.start_as_current_span("parapetai.tool_call"):
        hook.evaluate(snapshot=snapshot, stage="pre")

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["principal"] == 'Agent::"a1"'
    assert attrs["action"] == "tool_call"
    assert attrs["decision"] == "allow"
    assert attrs["tool_name"] == "lookup_order"
    assert attrs["tenant"] == "acme"
    # A dict value (not a valid OTel attribute type) is JSON-stringified,
    # not dropped or raised on.
    assert "order_id" in attrs["tool_args"]


def test_span_attributes_strip_content_bearing_keys(tmp_path: Path) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(
        provider="openai",
        endpoint="in-process:test:model_call",
        parsed=True,
        messages_preview="hello, my SSN is 123-45-6789",
    )

    with tracer.start_as_current_span("parapetai.model_call"):
        hook.evaluate(snapshot=snapshot, stage="pre")

    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert "messages_preview" not in attrs


def test_span_attributes_are_a_safe_noop_without_a_recording_tracer(tmp_path: Path) -> None:
    """No configured tracer -- get_current_span() returns a non-recording
    span (OTel's own global default). Must not raise; this is what makes
    the feature safe for a caller like the standalone gateway that has no
    OTel wiring at all."""
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    hook = GovernanceHook(engine, caller)
    snapshot = Snapshot(provider="openai", endpoint="in-process:test:model_call", parsed=True)

    result = hook.evaluate(snapshot=snapshot, stage="pre")  # must not raise
    assert result.decision.allowed


def test_custom_on_decision_called_exactly_once(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    caller = Caller(agent_id="a1", tenant="default")
    calls: list[Decision] = []

    def on_decision(
        decision: Decision, principal: str, snapshot: Snapshot, resource: str, context: dict
    ) -> None:  # noqa: E501
        calls.append(decision)

    hook = GovernanceHook(engine, caller, on_decision=on_decision)
    snapshot = Snapshot(provider="openai", endpoint="in-process:maf:model_call", parsed=True)

    hook.evaluate(snapshot=snapshot, stage="pre")
    assert len(calls) == 1
