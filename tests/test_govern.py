"""Framework-neutral govern() facade (parapetai_agent.govern).

Proves the same Cedar decisions the MAF adapter makes are reachable with no
framework at all: a tool call is authorized by name/args/role, a denial raises
GovernanceDenied (so a wrapped tool never runs), and raise_on_deny=False hands
back the Decision instead. Runs against the shipped example policies/ bundle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parapetai_agent import GovernanceDenied, Governor

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


class TestCheckInput:
    def test_ordinary_prompt_allowed(self) -> None:
        d = _gov().check_input("What is the status of order A1001?")
        assert d.allowed is True


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
