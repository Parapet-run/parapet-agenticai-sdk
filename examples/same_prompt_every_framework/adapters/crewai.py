"""CrewAI -- same @gov.tool seam as the OpenAI Agents SDK."""

from __future__ import annotations

import os

from ._shared import (
    INSTRUCTIONS,
    MODEL,
    POLICIES,
    delete_incident_body,
    lookup_incident_body,
)

NAME = "crewai"
INTEGRATION = "@gov.tool                    # under @crewai.tools.tool"


def available() -> bool:
    try:
        import crewai  # noqa: F401
    except ImportError:
        return False
    return True


async def run(prompt: str, ran: dict[str, bool], tool_name: str) -> None:
    # CrewAI phones home unless told not to; do it before importing the package.
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")

    from crewai import LLM, Agent, Crew, Task
    from crewai.tools import tool as crew_tool

    from parapetai_agent import Governor

    gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")

    @crew_tool("lookup_incident")
    @gov.tool
    def lookup_incident(number: str = "INC0010026") -> str:
        """Look up an incident's current status."""
        ran["lookup_incident"] = True
        return lookup_incident_body(number)

    @crew_tool("delete_incident")
    @gov.tool
    def delete_incident(number: str = "INC0010026") -> str:
        """Permanently delete an incident record."""
        ran["delete_incident"] = True
        return delete_incident_body(number)

    tool = {"lookup_incident": lookup_incident, "delete_incident": delete_incident}[tool_name]

    agent = Agent(
        role="IT service desk",
        goal=INSTRUCTIONS,
        backstory="You work the service desk.",
        tools=[tool],
        llm=LLM(model=f"anthropic/{MODEL}"),
        verbose=False,
    )
    crew = Crew(
        agents=[agent],
        tasks=[Task(description=prompt, expected_output="A short answer.", agent=agent)],
        verbose=False,
    )
    # kickoff() refuses to run inside an existing event loop, and this demo
    # drives every framework from one async orchestrator.
    await crew.kickoff_async()
