"""Google ADK -- the seam is the Runner, not the Agent.

ADK puts governance on Runner(plugins=[...]), so the drop-in replacement is
the Runner class. Same governance, different attachment point -- a real
architectural difference between the frameworks, not an inconsistency here.
"""

from __future__ import annotations

from ._shared import (
    INSTRUCTIONS,
    MODEL,
    POLICIES,
    delete_incident_body,
    lookup_incident_body,
)

NAME = "adk"
INTEGRATION = "InMemoryGovernedRunner(...)  # was: adk.runners.InMemoryRunner"


def available() -> bool:
    try:
        import google.adk  # noqa: F401
        import litellm  # noqa: F401
    except ImportError:
        return False
    return True


async def run(prompt: str, ran: dict[str, bool], tool_name: str) -> None:
    from google.adk.agents import LlmAgent
    from google.adk.models.lite_llm import LiteLlm
    from google.genai import types

    from parapetai_agent.adk import InMemoryGovernedRunner

    def lookup_incident(number: str = "INC0010026") -> str:
        """Look up an incident's current status."""
        ran["lookup_incident"] = True
        return lookup_incident_body(number)

    def delete_incident(number: str = "INC0010026") -> str:
        """Permanently delete an incident record."""
        ran["delete_incident"] = True
        return delete_incident_body(number)

    tool = {"lookup_incident": lookup_incident, "delete_incident": delete_incident}[tool_name]

    agent = LlmAgent(
        name="servicedesk",
        model=LiteLlm(model=f"anthropic/{MODEL}"),
        instruction=INSTRUCTIONS,
        tools=[tool],
    )
    runner = InMemoryGovernedRunner(
        agent=agent,
        app_name="servicedesk",
        policy_dir=POLICIES,
        entities_path=POLICIES / "entities.json",
        agent_id="demo-adk",
    )
    session = await runner.session_service.create_session(
        app_name="servicedesk", user_id="operator"
    )
    async for _event in runner.run_async(
        user_id="operator",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        pass
