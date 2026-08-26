"""Minimal CrewAI agent with one tool. No gateway-specific code -- that is the test."""

import os

from crewai import LLM, Agent, Crew, Task
from crewai.tools import tool


@tool("lookup_order")
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


# custom_openai=True forces the native OpenAI provider (respecting
# OPENAI_API_BASE/OPENAI_BASE_URL) regardless of what the model name looks
# like. Without it, CrewAI infers a provider from the model name itself --
# for a Groq model name that would route straight to Groq via LiteLLM,
# bypassing the gateway entirely. See CLAUDE.md TRAP note on CrewAI.
llm = LLM(model=os.environ["CONFORMANCE_MODEL"], custom_openai=True)

agent = Agent(
    role="Support agent",
    goal="Use the tool.",
    backstory="You look up orders for customers.",
    tools=[lookup_order],
    llm=llm,
)

task = Task(
    description="Look up order 12345",
    expected_output="The order status.",
    agent=agent,
)

result = Crew(agents=[agent], tasks=[task]).kickoff()
print(result)
