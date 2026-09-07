"""parapetai_agent.langgraph -- ParapetAgentMiddleware / build_middleware().

Mirrors the contract test_maf.py/test_adk.py hold their own adapters to:
real construction, real ALLOW/DENY at every stage this adapter implements
(model_call pre, tool_call, model_call post), ambient identity, and the
idempotent per-identity registry. Skips cleanly (pytest.importorskip) when
the `langgraph` extra (the full `langchain` package) isn't installed --
same guard tests/test_conformance_frameworks.py's own TestLangGraph uses,
since neither is a `dev`-extra dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parapetai_agent import GovernanceDenied
from parapetai_agent.scoped_data import governed_identity

pytest.importorskip("langchain")

from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from parapetai_agent.langgraph import (  # noqa: E402
    ParapetAgentMiddleware,
    build_middleware,
    reset_middleware_registry,
)


class _FakeModel(GenericFakeChatModel):
    """create_agent calls model.bind_tools(...) unconditionally;
    GenericFakeChatModel doesn't implement it (a pure scripted-response
    test double, no real tool-schema binding to do) -- same fix
    examples/langgraph_identity_scoped.py already documents."""

    def bind_tools(self, tools: object, **kwargs: object) -> _FakeModel:
        return self


def _write(policy_dir: Path, name: str, text: str) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / name).write_text(text)


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order {order_id}: shipped"


@tool
def execute_shell(command: str) -> str:
    """Run a shell command."""
    return f"ran: {command}"


@tool
def delete_salesforce_case(case_id: str) -> str:
    """Delete a Salesforce case."""
    return case_id


# LangChain's @tool decorator has no metadata= kwarg -- set post-hoc, same
# as a real caller would (delete_salesforce_case.metadata = {...}).
delete_salesforce_case.metadata = {
    "parapet_vendor_system": "salesforce",
    "parapet_resource_type": "Case",
    "parapet_crud_action": "delete",
}


def _agent(tool_name: str, args: dict, middleware: ParapetAgentMiddleware):
    model = _FakeModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
                AIMessage(content="Done."),
            ]
        )
    )
    return create_agent(
        model, tools=[lookup_order, execute_shell, delete_salesforce_case], middleware=[middleware]
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_middleware_registry()
    yield
    reset_middleware_registry()


def test_build_middleware_returns_parapet_agent_middleware(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", 'permit(principal, action == Action::"tool_call", resource);')
    mw = build_middleware(policy_dir=str(tmp_path))
    assert isinstance(mw, ParapetAgentMiddleware)


def test_tool_call_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);',
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    agent = _agent("lookup_order", {"order_id": "A1"}, mw)
    result = agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert result["messages"][-1].content == "Done."


def test_tool_call_denied_raises_and_never_runs(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("no_shell")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has tool_name && context.tool_name == "execute_shell" };',
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    agent = _agent("execute_shell", {"command": "rm -rf /"}, mw)
    with pytest.raises(GovernanceDenied) as exc_info:
        agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    # determining_policies is cedarpy's own RAW positional id (e.g.
    # "policy2"), not the @id("no_shell") annotation -- the @id-to-label
    # resolution is a private helper used only for a REVIEW decision's
    # reason text (policy/engine.py's _policy_labels()), not for a plain
    # hard deny like this one. Assert the shape, not a specific literal.
    assert len(exc_info.value.decision.determining_policies) == 1


def test_tool_call_reads_vendor_metadata_off_request_tool(tmp_path: Path) -> None:
    """Finding #3: _tool_snapshot used to read only request.tool_call (the
    raw {name, args, id} dict), never request.tool -- so a LangChain
    tool's own `.metadata` was completely unreachable by any Cedar
    decision. A forbid keyed on context.crud_action can only fire if
    _tool_snapshot actually resolved it from request.tool.metadata."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("no_deletes")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has crud_action && context.crud_action == "delete" };',
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    agent = _agent("delete_salesforce_case", {"case_id": "500x"}, mw)
    with pytest.raises(GovernanceDenied) as exc_info:
        agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert len(exc_info.value.decision.determining_policies) == 1


def test_model_call_pre_denied_raises_before_model_ever_runs(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        '@id("no_model")\nforbid(principal, action == Action::"model_call", resource);',
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    model = _FakeModel(messages=iter([AIMessage(content="should never be reached")]))
    agent = create_agent(model, tools=[], middleware=[mw])
    with pytest.raises(GovernanceDenied) as exc_info:
        agent.invoke({"messages": [{"role": "user", "content": "hello"}]})
    assert exc_info.value.decision.effect == "deny"


def test_model_call_post_denied_on_response_content(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        '@id("no_leak")\n@stage("post")\n'
        'forbid(principal, action == Action::"model_call", resource)\n'
        "when { context has response_preview && "
        'context.response_preview like "*SECRET*" };',
    )
    mw = build_middleware(policy_dir=str(tmp_path))

    denied_model = _FakeModel(messages=iter([AIMessage(content="the SECRET is out")]))
    denied_agent = create_agent(denied_model, tools=[], middleware=[mw])
    with pytest.raises(GovernanceDenied) as exc_info:
        denied_agent.invoke({"messages": [{"role": "user", "content": "hello"}]})
    # Same caveat as the tool-call test above: raw positional id, not @id.
    assert len(exc_info.value.decision.determining_policies) == 1

    allowed_model = _FakeModel(messages=iter([AIMessage(content="everything is fine")]))
    allowed_agent = create_agent(allowed_model, tools=[], middleware=[mw])
    result = allowed_agent.invoke({"messages": [{"role": "user", "content": "hello"}]})
    assert result["messages"][-1].content == "everything is fine"


def test_ambient_identity_via_governed_identity_scopes_tool_access(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("order_viewer_only")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        "when {\n"
        '  context has tool_name && context.tool_name == "lookup_order" &&\n'
        "  context has identity_roles &&\n"
        '  !context.identity_roles.contains("OrderViewer")\n'
        "};",
    )
    mw = build_middleware(policy_dir=str(tmp_path))

    allowed_agent = _agent("lookup_order", {"order_id": "A1"}, mw)
    with governed_identity(claims={"name": "Tony"}, roles=["OrderViewer"]):
        result = allowed_agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert result["messages"][-1].content == "Done."

    denied_agent = _agent("lookup_order", {"order_id": "A1"}, mw)
    with pytest.raises(GovernanceDenied):
        with governed_identity(claims={"name": "Sally"}, roles=["IncidentViewer"]):
            denied_agent.invoke({"messages": [{"role": "user", "content": "x"}]})


def test_build_middleware_is_idempotent_per_identity(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", 'permit(principal, action == Action::"tool_call", resource);')
    first = build_middleware(policy_dir=str(tmp_path), entities_path=None)
    second = build_middleware(policy_dir=str(tmp_path), entities_path=None)
    assert first is second


def test_build_middleware_different_agent_id_is_a_different_instance(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", 'permit(principal, action == Action::"tool_call", resource);')
    first = build_middleware(policy_dir=str(tmp_path), agent_id="agent-a")
    second = build_middleware(policy_dir=str(tmp_path), agent_id="agent-b")
    assert first is not second


def test_reset_middleware_registry_clears_the_cache(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", 'permit(principal, action == Action::"tool_call", resource);')
    first = build_middleware(policy_dir=str(tmp_path))
    reset_middleware_registry()
    second = build_middleware(policy_dir=str(tmp_path))
    assert first is not second


def test_no_policy_dir_uses_bundled_default_and_still_governs(tmp_path: Path) -> None:
    # Omitting policy_dir entirely falls back to the SDK's own bundled
    # default policy (permit model_call/tool_call broadly) -- zero-kwarg
    # construction still enforces something real, never zero enforcement.
    mw = build_middleware(agent_id="bundled-default-test")
    agent = _agent("lookup_order", {"order_id": "A1"}, mw)
    result = agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert result["messages"][-1].content == "Done."


# ── Cumulative cost/token tracking (docs/reference/cost-tracking.md) ────────


def _heavy_agent(mw: ParapetAgentMiddleware):
    """A two-turn tool-calling run whose FIRST model call reports 160
    tokens of usage -- enough to cross a 100-token budget by the time the
    SECOND model call's pre-stage decision (or the tool_call in between)
    evaluates."""
    model = _FakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup_order", "args": {"order_id": "A1"}, "id": "c1"}],
                    usage_metadata={"input_tokens": 80, "output_tokens": 80, "total_tokens": 160},
                ),
                AIMessage(content="Done."),
            ]
        )
    )
    return create_agent(
        model, tools=[lookup_order, execute_shell, delete_salesforce_case], middleware=[mw]
    )


def test_trace_cumulative_tokens_denies_a_later_turn_once_over_budget(tmp_path: Path) -> None:
    """Proves accumulation actually crosses a turn boundary: the FIRST
    model_call's pre-stage decision must be allowed (nothing spent yet),
    its own usage recorded after the response comes back, and the SECOND
    model_call's pre-stage decision (a separate, later wrap_model_call
    invocation, not nested in the first) must see the running total."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("token_budget")\n'
        'forbid(principal, action == Action::"model_call", resource)\n'
        "when { context has trace_cumulative_tokens && context.trace_cumulative_tokens > 100 };",
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    agent = _heavy_agent(mw)
    with pytest.raises(GovernanceDenied) as exc_info:
        agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert exc_info.value.decision.effect == "deny"


def test_tool_call_sees_span_cumulative_tokens_from_its_triggering_model_call(
    tmp_path: Path,
) -> None:
    """The tool_call this turn triggers must accumulate into the SAME
    "turn" total as the model_call that requested it (mirrors maf.py's own
    COST-TRACK-1 correlation) -- span_cumulative_tokens, not
    trace_cumulative_tokens, since this is a single-turn budget."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        '@id("span_budget")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        "when { context has span_cumulative_tokens && context.span_cumulative_tokens > 100 };",
    )
    mw = build_middleware(policy_dir=str(tmp_path))
    agent = _heavy_agent(mw)
    with pytest.raises(GovernanceDenied) as exc_info:
        agent.invoke({"messages": [{"role": "user", "content": "x"}]})
    assert exc_info.value.decision.effect == "deny"


def test_new_agent_invoke_starts_a_fresh_trace(tmp_path: Path) -> None:
    """before_agent must reset the trace scope: a SEPARATE, later
    agent.invoke() sharing the same middleware instance must not inherit
    cumulative usage from a previous, unrelated run."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
        '@id("token_budget")\n'
        'forbid(principal, action == Action::"model_call", resource)\n'
        "when { context has trace_cumulative_tokens && context.trace_cumulative_tokens > 100 };",
    )
    mw = build_middleware(policy_dir=str(tmp_path))

    heavy_agent = _heavy_agent(mw)
    with pytest.raises(GovernanceDenied):
        heavy_agent.invoke({"messages": [{"role": "user", "content": "x"}]})

    light_model = _FakeModel(messages=iter([AIMessage(content="Hi.")]))
    light_agent = create_agent(light_model, tools=[], middleware=[mw])
    result = light_agent.invoke({"messages": [{"role": "user", "content": "hello"}]})
    assert result["messages"][-1].content == "Hi."
