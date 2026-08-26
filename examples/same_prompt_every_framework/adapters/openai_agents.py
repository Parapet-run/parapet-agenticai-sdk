"""OpenAI Agents SDK -- no adapter class; govern the tool itself."""

from __future__ import annotations

from ._shared import (
    INSTRUCTIONS,
    MODEL,
    POLICIES,
    delete_incident_body,
    lookup_incident_body,
)

NAME = "openai-agents"
INTEGRATION = "@gov.tool                    # under @function_tool"


def available() -> bool:
    try:
        import agents  # noqa: F401
        import litellm  # noqa: F401
    except ImportError:
        return False
    return True


async def run(prompt: str, ran: dict[str, bool], tool_name: str) -> None:
    from agents import Agent, Runner, function_tool, set_tracing_disabled
    from agents.extensions.models.litellm_model import LitellmModel

    from parapetai_agent import Governor

    set_tracing_disabled(True)
    gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")

    @function_tool
    @gov.tool
    def lookup_incident(number: str = "INC0010026") -> str:
        """Look up an incident's current status."""
        ran["lookup_incident"] = True
        return lookup_incident_body(number)

    @function_tool
    @gov.tool
    def delete_incident(number: str = "INC0010026") -> str:
        """Permanently delete an incident record."""
        ran["delete_incident"] = True
        return delete_incident_body(number)

    tool = {"lookup_incident": lookup_incident, "delete_incident": delete_incident}[tool_name]

    agent = Agent(
        name="servicedesk",
        instructions=INSTRUCTIONS,
        tools=[tool],
        model=LitellmModel(model=f"anthropic/{MODEL}"),
    )
    await Runner.run(agent, prompt)
