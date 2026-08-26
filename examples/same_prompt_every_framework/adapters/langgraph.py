"""LangGraph -- same @gov.tool seam, wrapped by LangChain's own @tool."""

from __future__ import annotations

from ._shared import (
    INSTRUCTIONS,
    MODEL,
    POLICIES,
    delete_incident_body,
    lookup_incident_body,
)

NAME = "langgraph"
INTEGRATION = "@gov.tool                    # under langchain_core @tool"


def available() -> bool:
    try:
        import langchain_anthropic  # noqa: F401
        import langgraph  # noqa: F401
    except ImportError:
        return False
    return True


async def run(prompt: str, ran: dict[str, bool], tool_name: str) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.tools import tool as lc_tool
    from langgraph.prebuilt import create_react_agent

    from parapetai_agent import Governor

    gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")

    @lc_tool
    @gov.tool
    def lookup_incident(number: str = "INC0010026") -> str:
        """Look up an incident's current status."""
        ran["lookup_incident"] = True
        return lookup_incident_body(number)

    @lc_tool
    @gov.tool
    def delete_incident(number: str = "INC0010026") -> str:
        """Permanently delete an incident record."""
        ran["delete_incident"] = True
        return delete_incident_body(number)

    tool = {"lookup_incident": lookup_incident, "delete_incident": delete_incident}[tool_name]

    agent = create_react_agent(
        ChatAnthropic(model=MODEL),
        tools=[tool],
        prompt=INSTRUCTIONS,
    )
    await agent.ainvoke({"messages": [("user", prompt)]})
