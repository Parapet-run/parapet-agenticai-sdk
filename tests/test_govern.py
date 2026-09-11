"""Framework-neutral govern() facade (parapetai_agent.govern).

Proves the same Cedar decisions the MAF adapter makes are reachable with no
framework at all: a tool call is authorized by name/args/role, a denial raises
GovernanceDenied (so a wrapped tool never runs), and raise_on_deny=False hands
back the Decision instead. Runs against the shipped example policies/ bundle.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from parapetai_agent import GovernanceDenied, Governor
from parapetai_agent.policy.engine import Decision
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.vendor_calls import VendorCallSpec, declare_vendor_call

POLICIES = Path(__file__).resolve().parents[1] / "policies"


def _gov() -> Governor:
    return Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")


class TestAuthorizeTool:
    def test_read_tool_is_allowed(self) -> None:
        d = _gov().authorize_tool("lookup_order", {"order_id": "A1001"})
        assert d.allowed is True

    def test_destructive_tool_is_denied(self) -> None:
        with pytest.raises(GovernanceDenied) as exc:
            _gov().authorize_tool("delete_incident", {"number": "INC1"})
        assert exc.value.decision.allowed is False
        assert exc.value.decision.effect == "deny"
        # a forbid fired (a determining policy is named); Cedar surfaces its
        # positional id (e.g. "policy5"), not the @id annotation.
        assert exc.value.decision.determining_policies

    def test_denied_by_argument(self) -> None:
        # closing an incident via a raw state update is denied on the argument
        with pytest.raises(GovernanceDenied):
            _gov().authorize_tool("update_incident", {"number": "INC1", "state": "closed"})
        # same tool, benign argument: allowed
        d = _gov().authorize_tool("update_incident", {"number": "INC1", "state": "in_progress"})
        assert d.allowed is True

    def test_role_gate(self) -> None:
        gov = _gov()
        # a caller asserting roles but lacking OrderViewer is denied lookup_order
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("lookup_order", {"order_id": "A1001"}, roles=["SomethingElse"])
        # with the role, allowed
        d = gov.authorize_tool("lookup_order", {"order_id": "A1001"}, roles=["OrderViewer"])
        assert d.allowed is True

    def test_raise_on_deny_false_returns_decision(self) -> None:
        d = _gov().authorize_tool("execute_shell", {"command": "rm -rf /"}, raise_on_deny=False)
        assert d.allowed is False
        assert d.effect == "deny"


class TestOtelSpans:
    """auth-integrations.md §10.7: check_input()/authorize_tool() previously
    opened no OTel span at all -- Governor had no framework loop to hook a
    real `with start_as_current_span():` block around, since both are
    standalone calls (the real tool/model call happens in the CALLER's own
    code, not inside anything Governor controls). Proven the same way
    test_maf.py's own OTelCorrelation test proves it for MAF: a real
    TracerProvider + InMemorySpanExporter, real check_input()/
    authorize_tool() calls, asserting real spans came out."""

    def test_check_input_and_authorize_tool_open_real_correlated_spans(self) -> None:
        # All three scenarios share ONE TracerProvider/exporter, cleared
        # between them with exporter.clear() rather than each getting its
        # own fresh TracerProvider -- confirmed live that opentelemetry's
        # own ProxyTracer (what `trace.get_tracer(__name__)` returns, and
        # what govern.py's module-level `_tracer` is) permanently CACHES
        # the first real Tracer it resolves against
        # (`if self._real_tracer: return self._real_tracer`, read directly
        # from the installed opentelemetry-api source) and never re-checks
        # `_TRACER_PROVIDER` again -- so a SECOND `set_tracer_provider()`
        # call later in the same process is silently invisible to an
        # already-resolved ProxyTracer, even though conftest.py's
        # `_reset_otel_module_state` correctly resets the global. This is
        # a real property of upstream OTel, not something to work around
        # in govern.py itself (a real embedding process only ever sets one
        # real provider once); it just means a test suite has to keep
        # provider identity stable across scenarios that share a module's
        # already-resolved `_tracer`, hence one shared setup below instead
        # of one fresh TracerProvider per test method.
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        otel_trace.set_tracer_provider(tracer_provider)

        # --- Scenario 1: check_input() + authorize_tool() correlate ---
        gov = _gov()
        with gov.trace():
            gov.check_input("What is the status of order A1001?")
            gov.authorize_tool("lookup_order", {"order_id": "A1001"})

        all_spans = span_exporter.get_finished_spans()
        model_spans = [s for s in all_spans if s.name == "parapetai.model_call"]
        tool_spans = [s for s in all_spans if s.name == "parapetai.tool_call"]
        assert len(model_spans) == 1
        assert len(tool_spans) == 1
        # Same-context, single-threaded, no framework executor hop between
        # check_input() and authorize_tool() here (unlike langgraph.py's
        # own Pregel-dispatched case) -- real parent correlation IS the
        # achievable, tested property for Governor's own standalone-call
        # shape.
        tool_span = tool_spans[0]
        assert tool_span.parent is not None
        assert tool_span.parent.span_id == model_spans[0].context.span_id
        assert tool_span.context.trace_id == model_spans[0].context.trace_id

        # --- Scenario 2: a denial is reflected on the span's status ---
        span_exporter.clear()
        with gov.trace(), pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_incident", {"number": "INC1"})
        (denied_span,) = [
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.tool_call"
        ]
        assert denied_span.status.status_code == otel_trace.StatusCode.ERROR

        # --- Scenario 3: authorize_tool() alone (no check_input()) gets a
        # root span -- per this module's own top-of-file example, a
        # caller that only ever governs tool calls is valid and common;
        # must not require check_input() first, and must not raise trying
        # to parent to a model_call span that was never opened. ---
        span_exporter.clear()
        with gov.trace():
            gov.authorize_tool("lookup_order", {"order_id": "A1001"})
        (root_span,) = [
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.tool_call"
        ]
        assert root_span.parent is None


class TestCheckInput:
    def test_ordinary_prompt_allowed(self) -> None:
        d = _gov().check_input("What is the status of order A1001?")
        assert d.allowed is True


def _gov_capturing() -> tuple[Governor, list[dict[str, Any]]]:
    contexts: list[dict[str, Any]] = []

    def _capture(
        decision: Decision,
        principal: str,
        snapshot: Snapshot,
        resource: str,
        context: Mapping[str, Any],
    ) -> None:
        contexts.append(dict(context))

    return (
        Governor.from_policy_dir(POLICIES, POLICIES / "entities.json", on_decision=_capture),
        contexts,
    )


class TestAgentIdentityClaims:
    """agent_claims is distinct from claims (the end user's) -- both must
    reach the decision context separately, on all three of check_input()/
    authorize_tool()/check_output(), not just whichever one was fixed
    first."""

    def test_check_input_carries_both_end_user_and_agent_claims(self) -> None:
        gov, contexts = _gov_capturing()
        gov.check_input(
            "hello",
            claims={"oid": "user-42"},
            agent_claims={"sub": "agent-sp-1", "iss": "https://agent-idp.example"},
        )
        assert contexts[-1]["identity_claims"] == {"oid": "user-42"}
        assert contexts[-1]["agent_identity_claims"] == {
            "sub": "agent-sp-1",
            "iss": "https://agent-idp.example",
        }

    def test_authorize_tool_carries_agent_claims(self) -> None:
        # raise_on_deny=False: the shipped policies/ bundle's own role gate
        # (policies/30-identity.cedar) may deny lookup_order for an
        # identity_claims.oid with no matching role -- irrelevant to this
        # test, which only checks that agent_identity_claims reached the
        # context on_decision sees, not the allow/deny outcome itself.
        gov, contexts = _gov_capturing()
        gov.authorize_tool(
            "lookup_order",
            {"order_id": "A1001"},
            claims={"oid": "user-42"},
            agent_claims={"client_id": "agent-app-id"},
            raise_on_deny=False,
        )
        assert contexts[-1]["agent_identity_claims"] == {"client_id": "agent-app-id"}

    def test_check_output_carries_agent_claims(self) -> None:
        gov, contexts = _gov_capturing()
        gov.check_output(
            "order shipped",
            claims={"oid": "user-42"},
            agent_claims={"sub": "agent-sp-1"},
        )
        assert contexts[-1]["agent_identity_claims"] == {"sub": "agent-sp-1"}

    def test_absent_agent_claims_omits_the_key_entirely(self) -> None:
        # Snapshot.to_context() only adds agent_identity_claims `if
        # self.agent_identity_claims:` -- an empty dict must not appear as
        # a present-but-empty key (same has-check reasoning identity_claims
        # itself already documents).
        gov, contexts = _gov_capturing()
        gov.check_input("hello", claims={"oid": "user-42"})
        assert "agent_identity_claims" not in contexts[-1]


class TestToolDecorator:
    def test_decorator_blocks_denied_tool_before_it_runs(self) -> None:
        gov = _gov()
        ran = {"delete": False}

        @gov.tool
        def delete_incident(number: str) -> str:
            ran["delete"] = True
            return "deleted"

        with pytest.raises(GovernanceDenied):
            delete_incident(number="INC1")
        assert ran["delete"] is False  # body never executed

    def test_decorator_runs_allowed_tool(self) -> None:
        gov = _gov()

        @gov.tool
        def lookup_order(order_id: str) -> str:
            return f"order {order_id}: shipped"

        assert lookup_order(order_id="A1001") == "order A1001: shipped"

    async def test_decorator_on_async_tool(self) -> None:
        gov = _gov()

        @gov.tool
        async def delete_incident(number: str) -> str:
            return "deleted"

        with pytest.raises(GovernanceDenied):
            await delete_incident(number="INC1")


def test_governor_and_exception_import_from_base_package() -> None:
    # both must be importable with no agent_framework installed (base install)
    import parapetai_agent

    assert "Governor" in parapetai_agent.__all__
    assert "GovernanceDenied" in parapetai_agent.__all__


def _write(policy_dir: Path, name: str, text: str) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / name).write_text(text)


class TestVendorMetadata:
    """authorize_tool()'s func=/metadata= resolve the SAME
    context.vendor_system/crud_action fields adk.py/langgraph.py produce --
    mirrors test_langgraph.py::test_tool_call_reads_vendor_metadata_off_request_tool,
    proving Governor reaches the identical Cedar context, not a
    Governor-specific approximation of it."""

    POLICY = (
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("no_deletes")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has crud_action && context.crud_action == "delete" };'
    )

    def test_metadata_dict_reaches_cedar(self, tmp_path: Path) -> None:
        _write(tmp_path, "00-base.cedar", self.POLICY)
        gov = Governor.from_policy_dir(tmp_path)
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool(
                "salesforce_request",
                {"case_id": "500x"},
                metadata={
                    "parapet_vendor_system": "salesforce",
                    "parapet_resource_type": "Case",
                    "parapet_crud_action": "delete",
                },
            )

    def test_undeclared_tool_is_unaffected(self, tmp_path: Path) -> None:
        _write(tmp_path, "00-base.cedar", self.POLICY)
        gov = Governor.from_policy_dir(tmp_path)
        d = gov.authorize_tool("lookup_order", {"order_id": "A1001"})
        assert d.allowed is True

    def test_declare_vendor_call_func_reaches_cedar(self, tmp_path: Path) -> None:
        _write(tmp_path, "00-base.cedar", self.POLICY)
        gov = Governor.from_policy_dir(tmp_path)

        @declare_vendor_call(
            VendorCallSpec(vendor_system="salesforce", resource_type="Case", crud_action="delete")
        )
        def delete_salesforce_case(case_id: str) -> str:
            return case_id

        with pytest.raises(GovernanceDenied):
            gov.authorize_tool(
                "delete_salesforce_case", {"case_id": "500x"}, func=delete_salesforce_case
            )

    def test_tool_decorator_resolves_declare_vendor_call_automatically(
        self, tmp_path: Path
    ) -> None:
        """Governor.tool() must pass func=f itself -- a caller shouldn't
        have to also call authorize_tool() by hand just to get vendor
        metadata resolved."""
        _write(tmp_path, "00-base.cedar", self.POLICY)
        gov = Governor.from_policy_dir(tmp_path)
        ran = {"delete": False}

        @gov.tool
        @declare_vendor_call(
            VendorCallSpec(vendor_system="salesforce", resource_type="Case", crud_action="delete")
        )
        def delete_salesforce_case(case_id: str) -> str:
            ran["delete"] = True
            return case_id

        with pytest.raises(GovernanceDenied):
            delete_salesforce_case(case_id="500x")
        assert ran["delete"] is False


class TestVendorScopedResources:
    """vendor_scoped_resources=True switches Cedar's `resource` itself,
    same behavior maf.py/adk.py/langgraph.py already have -- proves
    Governor.from_policy_dir()'s new kwarg actually reaches
    GovernanceHook, not just accepted and dropped."""

    def test_declared_tool_resolves_vendor_scoped_resource(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "00-base.cedar",
            'permit(principal, action == Action::"model_call", resource);\n'
            '@id("scoped_permit")\n'
            'permit(principal, action == Action::"tool_call", resource)\n'
            'when { resource == Resource::"salesforce/Case.delete" };',
        )
        gov = Governor.from_policy_dir(tmp_path, vendor_scoped_resources=True)

        @declare_vendor_call(
            VendorCallSpec(vendor_system="salesforce", resource_type="Case", crud_action="delete")
        )
        def delete_salesforce_case(case_id: str) -> str:
            return case_id

        d = gov.authorize_tool(
            "delete_salesforce_case", {"case_id": "500x"}, func=delete_salesforce_case
        )
        assert d.allowed is True

    def test_undeclared_tool_resolves_to_the_undeclared_resource(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "00-base.cedar",
            'permit(principal, action == Action::"model_call", resource);\n'
            '@id("no_undeclared")\n'
            'forbid(principal, action == Action::"tool_call", resource)\n'
            'when { resource == Resource::"undeclared" };',
        )
        gov = Governor.from_policy_dir(tmp_path, vendor_scoped_resources=True)
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("lookup_order", {"order_id": "A1001"})

    def test_off_by_default_keeps_provider_scoped_resource(self, tmp_path: Path) -> None:
        # Same undeclared-tool call, flag OFF: the "undeclared" forbid never
        # matches at all, since resource stays provider-scoped -- proves
        # the flag is truly opt-in, not silently always-on.
        _write(
            tmp_path,
            "00-base.cedar",
            'permit(principal, action == Action::"model_call", resource);\n'
            'permit(principal, action == Action::"tool_call", resource);\n'
            '@id("no_undeclared")\n'
            'forbid(principal, action == Action::"tool_call", resource)\n'
            'when { resource == Resource::"undeclared" };',
        )
        gov = Governor.from_policy_dir(tmp_path)
        d = gov.authorize_tool("lookup_order", {"order_id": "A1001"})
        assert d.allowed is True


class TestCostTracking:
    """check_input()/authorize_tool()/check_output() populate the SAME
    context.trace_cumulative_*/span_cumulative_* fields cost-tracking.md
    documents for MAF/ADK -- via on_decision, the one integration-agnostic
    capture point every adapter shares (see docs/reference/decision.md)."""

    def _gov_capturing(self, tmp_path: Path) -> tuple[Governor, list[dict[str, Any]]]:
        _write(
            tmp_path,
            "00-base.cedar",
            'permit(principal, action == Action::"model_call", resource);\n'
            'permit(principal, action == Action::"tool_call", resource);',
        )
        contexts: list[dict[str, Any]] = []

        def _capture(
            decision: Decision,
            principal: str,
            snapshot: Snapshot,
            resource: str,
            context: Mapping[str, Any],
        ) -> None:
            contexts.append(dict(context))

        return Governor.from_policy_dir(tmp_path, on_decision=_capture), contexts

    def test_fields_always_present_even_with_no_usage_reported(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        gov.check_input("hello")
        assert contexts[-1]["trace_cumulative_tokens"] == 0
        assert contexts[-1]["trace_cumulative_cost_usd_micros"] == 0

    def test_usage_accumulates_within_one_trace(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        with gov.trace():
            gov.check_input("hello", model="gpt-4o-mini")
            gov.check_output(
                "hi there", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=50
            )
            # A second turn in the SAME trace sees the first turn's usage.
            gov.check_input("again", model="gpt-4o-mini")
            second_turn_pre_context = contexts[-1]
        assert second_turn_pre_context["trace_cumulative_tokens"] == 150
        assert second_turn_pre_context["trace_cumulative_cost_usd_micros"] > 0

    def test_tool_call_shares_the_preceding_check_input_span(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        with gov.trace():
            gov.check_input("hello", model="gpt-4o-mini")
            gov.check_output("", model="gpt-4o-mini", prompt_tokens=10, completion_tokens=5)
            gov.authorize_tool("lookup_order", {"order_id": "A1001"})
            tool_context = contexts[-1]
        assert tool_context["span_cumulative_tokens"] == 15

    def test_a_new_trace_does_not_see_the_previous_traces_usage(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        with gov.trace():
            gov.check_input("hello", model="gpt-4o-mini")
            gov.check_output("hi", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=50)
        with gov.trace():
            gov.check_input("fresh trace", model="gpt-4o-mini")
            fresh_context = contexts[-1]
        assert fresh_context["trace_cumulative_tokens"] == 0

    def test_without_trace_each_call_is_its_own_one_off(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        gov.check_input("hello", model="gpt-4o-mini")
        gov.check_output("hi", model="gpt-4o-mini", prompt_tokens=100, completion_tokens=50)
        gov.check_input("unrelated", model="gpt-4o-mini")
        assert contexts[-1]["trace_cumulative_tokens"] == 0

    def test_unpriced_model_still_counts_tokens_with_zero_cost(self, tmp_path: Path) -> None:
        gov, contexts = self._gov_capturing(tmp_path)
        with gov.trace():
            gov.check_input("hello", model="totally-unknown-model")
            gov.check_output(
                "hi", model="totally-unknown-model", prompt_tokens=100, completion_tokens=50
            )
            gov.check_input("again", model="totally-unknown-model")
            second_turn_context = contexts[-1]
        assert second_turn_context["trace_cumulative_tokens"] == 150
        assert second_turn_context["trace_cumulative_cost_usd_micros"] == 0
