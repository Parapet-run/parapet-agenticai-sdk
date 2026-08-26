"""Microsoft Agent Framework -- the seam is the Agent."""

from __future__ import annotations

from ._shared import INSTRUCTIONS, MODEL, POLICIES, delete_incident_body, lookup_incident_body

NAME = "maf"
INTEGRATION = "GovernedAgent(...)           # was: agent_framework.Agent"


def available() -> bool:
    try:
        import agent_framework  # noqa: F401
        from agent_framework.anthropic import AnthropicClient  # noqa: F401
    except ImportError:
        return False
    return True


async def run(prompt: str, ran: dict[str, bool], tool_name: str) -> None:
    from agent_framework.anthropic import AnthropicClient

    from parapetai_agent import GovernedAgent

    def lookup_incident(number: str = "INC0010026") -> str:
        """Look up an incident's current status."""
        ran["lookup_incident"] = True
        return lookup_incident_body(number)

    def delete_incident(number: str = "INC0010026") -> str:
        """Permanently delete an incident record."""
        ran["delete_incident"] = True
        return delete_incident_body(number)

    tool = {"lookup_incident": lookup_incident, "delete_incident": delete_incident}[tool_name]

    async with GovernedAgent(
        client=AnthropicClient(model=MODEL),
        name="servicedesk",
        instructions=INSTRUCTIONS,
        tools=[tool],
        policy_dir=POLICIES,
        entities_path=POLICIES / "entities.json",
        agent_id="demo-maf",
    ) as agent:
        await agent.run(prompt)
