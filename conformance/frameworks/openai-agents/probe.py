"""Minimal agent with one tool. No gateway-specific code -- that is the test."""

import os

from agents import Agent, Runner, function_tool, set_default_openai_api

# Groq (and the fake upstream) implement Chat Completions, not the newer
# Responses API this SDK defaults to. Switching APIs is a one-line SDK
# setting, not gateway-specific code -- it has nothing to do with the gateway
# being in the loop.
set_default_openai_api("chat_completions")


@function_tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


agent = Agent(
    name="probe",
    instructions="Use the tool.",
    model=os.environ["CONFORMANCE_MODEL"],
    tools=[lookup_order],
)

result = Runner.run_sync(agent, "Look up order 12345")
print(result.final_output)
