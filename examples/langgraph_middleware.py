"""ParapetAgentMiddleware -- the dedicated LangGraph/LangChain adapter
(parapetai_agent.langgraph), not the framework-neutral Governor fallback
the other two langgraph_*.py examples in this directory use.

Unlike langgraph_tool_calling.py (explicit gov.check_input()/@gov.tool/
gov.check_output() calls, hand-wired at each site) and
langgraph_identity_scoped.py (roles threaded manually through
RunnableConfig), this example shows what a real
langchain.agents.middleware.AgentMiddleware buys: ONE middleware,
registered once at agent construction, covering the pre-model, tool-call,
and post-model Cedar decisions automatically -- plus ambient identity via
governed_identity(), the same contextvar-based mechanism MAF's/ADK's own
adapters already use, with no per-call config= plumbing required.

Reuses this repo's own root policies/ fixtures unchanged:
  - policies/20-tools.cedar: execute_shell is a hard-denied tool name
  - policies/30-identity.cedar: lookup_order requires the OrderViewer role

Requires: pip install "parapetai-agent[langgraph]"  (the full `langchain`
package -- AgentMiddleware lives in langchain.agents.middleware, not in
langgraph/langchain-core alone; see docs/frameworks/langgraph.md for why
create_agent, not the deprecated create_react_agent, is the target here).

Run:  uv run python examples/langgraph_middleware.py
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from parapetai_agent import GovernanceDenied
from parapetai_agent.langgraph import build_middleware
from parapetai_agent.scoped_data import governed_identity

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES = REPO_ROOT / "policies"

# One PolicyEngine, one middleware, reused across every scenario below --
# exactly the shape build_middleware()'s own idempotent registry expects.
middleware = build_middleware(policy_dir=POLICIES, entities_path=POLICIES / "entities.json")


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


@tool
def execute_shell(command: str) -> str:
    """Run a shell command."""
    return f"ran: {command}"  # pragma: no cover -- never reached, Cedar denies first


class _FakeModel(GenericFakeChatModel):
    """create_agent calls model.bind_tools(...) unconditionally;
    GenericFakeChatModel doesn't implement it -- see
    langgraph_identity_scoped.py's own docstring for the same, well-known
    LangChain testing gap and its standard fix."""

    def bind_tools(self, tools, **kwargs):
        return self


def _agent_for(tool_name: str, args: dict):
    model = _FakeModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
                AIMessage(content="Done."),
            ]
        )
    )
    return create_agent(model, tools=[lookup_order, execute_shell], middleware=[middleware])


def _run(label: str, tool_name: str, args: dict, roles: list[str] | None = None) -> None:
    agent = _agent_for(tool_name, args)
    try:
        if roles is not None:
            with governed_identity(claims={"name": label}, roles=roles):
                result = agent.invoke({"messages": [{"role": "user", "content": "x"}]})
        else:
            result = agent.invoke({"messages": [{"role": "user", "content": "x"}]})
        print(f"[{label:<28}] ALLOWED -> {result['messages'][-1].content!r}")
    except GovernanceDenied as exc:
        print(
            f"[{label:<28}] DENIED  -> {exc.decision.reason} "
            f"({exc.decision.determining_policies})"
        )


def main() -> None:
    print("Governed langchain.agents.create_agent -- ParapetAgentMiddleware\n")

    print("-- tool-call gating (policies/20-tools.cedar), no identity involved --")
    _run("lookup_order", "lookup_order", {"order_id": "A1001"})
    _run("execute_shell (denied)", "execute_shell", {"command": "rm -rf /"})

    print("\n-- ambient identity via governed_identity(), no RunnableConfig plumbing --")
    _run(
        "Tony/OrderViewer -> lookup_order",
        "lookup_order",
        {"order_id": "A1001"},
        roles=["OrderViewer"],
    )
    _run(
        "Sally/IncidentViewer -> lookup_order",
        "lookup_order",
        {"order_id": "A1001"},
        roles=["IncidentViewer"],
    )

    print(
        "\nOne middleware instance, registered once at construction, covered "
        "every scenario above -- no @gov.tool on execute_shell (it's still "
        "denied), no config={'configurable': {'roles': [...]}} at invoke() "
        "time (identity flowed from the ambient governed_identity() context "
        "instead). Compare against langgraph_tool_calling.py and "
        "langgraph_identity_scoped.py, which do this by hand."
    )


if __name__ == "__main__":
    main()
