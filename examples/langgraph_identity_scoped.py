"""LangGraph's prebuilt create_react_agent (the same prebuilt used by
tests/test_conformance_frameworks.py's TestLangGraph and
examples/same_prompt_every_framework/adapters/langgraph.py), extended to
close a gap those two leave open: per-END-USER identity.

Governor.authorize_tool()'s claims=/roles= are per-call explicit
arguments -- there is no ambient contextvar the way MAF's/ADK's own hook
functions read scoped_data's governed_identity() state automatically.
@gov.tool (see langgraph_tool_calling.py) never threads roles/claims at
all, so it can't enforce an identity-scoped Cedar policy like this repo's
own policies/30-identity.cedar (role-gated lookup_order/lookup_incident)
through a LangGraph agent.

This example closes that gap using LangGraph's OWN idiomatic mechanism for
per-invocation, per-user context -- a `config: RunnableConfig` tool
parameter, populated via `configurable=` at .invoke() time (the same
pattern the upstream LangGraph "Build a customer support bot" tutorial
uses to scope a passenger_id per call: langchain-ai/langgraph, docs/docs/
tutorials/customer-support/customer-support.ipynb). No new SDK code, no
ambient contextvar -- calling gov.authorize_tool(..., roles=...) directly
inside the tool body instead of using the @gov.tool decorator.

Two identities, one shared agent -- same story as the parapet-quickdemo
MCP skill (Tony/Sales vs. Sally/HR), using this repo's own root
policies/30-identity.cedar instead of a separate org policy:
  - Tony has the OrderViewer role  -> lookup_order allowed, lookup_incident denied
  - Sally has the IncidentViewer role -> lookup_incident allowed, lookup_order denied

Requires: pip install langgraph  (langchain-core comes along as its own
dependency). create_react_agent is deprecated as of langgraph 1.2/
langchain 1.3 in favor of langchain.agents.create_agent -- verified live,
still works and still the right thing to reach for here since it avoids a
dependency on the full `langchain` package for this one example; see
docs/frameworks/langgraph.md for why the newer create_agent's
AgentMiddleware (wrap_model_call/wrap_tool_call) matters for a future
dedicated adapter.

Run:  uv run python examples/langgraph_identity_scoped.py
"""

from __future__ import annotations

import warnings
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from parapetai_agent import GovernanceDenied, Governor

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES = REPO_ROOT / "policies"

gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")


@tool
def lookup_order(order_id: str, config: RunnableConfig) -> str:
    """Look up an order by id."""
    roles = config.get("configurable", {}).get("roles", [])
    gov.authorize_tool("lookup_order", {"order_id": order_id}, roles=roles)
    return f"order {order_id}: shipped"


@tool
def lookup_incident(number: str, config: RunnableConfig) -> str:
    """Look up an incident by number."""
    roles = config.get("configurable", {}).get("roles", [])
    gov.authorize_tool("lookup_incident", {"number": number}, roles=roles)
    return f"incident {number}: open"


class _ToolCallingFakeChatModel(GenericFakeChatModel):
    """create_react_agent calls model.bind_tools(...) unconditionally;
    GenericFakeChatModel doesn't implement it (NotImplementedError) since
    it's a pure scripted-response test double with no real tool-schema
    binding to do. Overriding it as a no-op is the standard fix for this
    exact, well-known LangChain testing gap -- our fake model's tool_calls
    come from the scripted AIMessage below regardless of what the model
    was "told" about available tools."""

    def bind_tools(self, tools, **kwargs):
        return self


def _agent_for(tool_name: str, args: dict):
    # Scripted one-turn model: decides to call exactly the tool this
    # scenario is testing. A real ChatOpenAI/ChatAnthropic would decide
    # this itself; the governance behavior below doesn't depend on how.
    model = _ToolCallingFakeChatModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
                AIMessage(content="Done."),
            ]
        )
    )
    # create_react_agent is deprecated as of langgraph 1.2/langchain 1.3 in
    # favor of langchain.agents.create_agent -- see this file's own module
    # docstring for why it's still the right call here. Silencing only
    # this specific, expected warning, not DeprecationWarning globally.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*create_react_agent.*")
        return create_react_agent(model, tools=[lookup_order, lookup_incident])


def _run(name: str, roles: list[str], tool_name: str, args: dict) -> None:
    agent = _agent_for(tool_name, args)
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": name}]},
            config={"configurable": {"roles": roles}},
        )
        print(f"[{name:<28}] roles={roles!r:<20} ALLOWED -> {result['messages'][-1].content!r}")
    except GovernanceDenied as exc:
        print(
            f"[{name:<28}] roles={roles!r:<20} DENIED  -> "
            f"{exc.decision.reason} ({exc.decision.determining_policies})"
        )


def main() -> None:
    print("Governed LangGraph create_react_agent -- identity scoped via RunnableConfig\n")
    _run("Tony -> lookup_order", ["OrderViewer"], "lookup_order", {"order_id": "A1001"})
    _run("Tony -> lookup_incident", ["OrderViewer"], "lookup_incident", {"number": "INC1"})
    _run("Sally -> lookup_incident", ["IncidentViewer"], "lookup_incident", {"number": "INC1"})
    _run("Sally -> lookup_order", ["IncidentViewer"], "lookup_order", {"order_id": "A1001"})
    print(
        "\nSame agent, same tools, same graph -- Cedar decided based on the "
        "role passed in via config={'configurable': {'roles': [...]}} at "
        "invoke() time, not anything hardcoded per identity."
    )


if __name__ == "__main__":
    main()
