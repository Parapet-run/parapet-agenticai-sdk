"""Minimal AutoGen agent with one tool. No gateway-specific code -- that is the test."""

import asyncio
import os

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient


def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


async def main() -> None:
    # No base_url kwarg: relies entirely on the underlying openai-python
    # client reading OPENAI_BASE_URL from the environment.
    #
    # model_info is required whenever the model name isn't in autogen's
    # built-in OpenAI model registry -- true for a Groq model name. Declaring
    # it here is a capability declaration for the client library, not
    # gateway-specific code.
    model_client = OpenAIChatCompletionClient(
        model=os.environ["CONFORMANCE_MODEL"],
        api_key=os.environ.get("OPENAI_API_KEY"),
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": False,
            "family": "unknown",
            "structured_output": False,
        },
    )
    agent = AssistantAgent(
        name="probe",
        model_client=model_client,
        tools=[lookup_order],
        system_message="Use the tool.",
    )
    result = await agent.run(task="Look up order 12345")
    print(result.messages[-1].content)
    await model_client.close()


asyncio.run(main())
