"""Live example: a governed agent with human-in-the-loop tool approval --
ported from Microsoft Agent Framework's own "Function Tool with Approval"
sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/tools/function_tool_with_approval.py).

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story. policy_dir/entities_path/agent_id/control_plane_url/
agent_secret are all omitted -- bundled default Cedar policy (base
permits) and env-var fallbacks apply.

Worth noting for this specific sample: approval_mode is a FRAMEWORK-level
gate (a human confirms before the function body ever runs) that composes
with, but is completely independent of, Cedar's own tool_call decision.
An agent framework approval and a Cedar permit answer two different
questions -- "should a human confirm this specific call" vs. "is this
agent allowed to call this tool at all" -- and this sample exercises both
gates on the SAME tool call (get_weather_detail), in either order: Cedar
can deny a call before it ever reaches the framework's approval prompt,
and a human can decline a call Cedar would have permitted.

Run (local dry run, governed by the bundled default policy, no control
plane needed):
    cp examples/maf_sample_05/.env.example examples/maf_sample_05/.env
    # fill in AZURE_OPENAI_* or OPENAI_API_KEY in that .env
    uv run --with agent-framework python3 examples/maf_sample_05/run_example.py

This one is interactive -- get_weather_detail (approval_mode="always_require")
prompts y/n on stdin before it runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from random import randrange
from typing import TYPE_CHECKING, Annotated, Any

from agent_framework import AgentResponse, Message, tool
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv

from parapetai_agent import GovernedAgent

if TYPE_CHECKING:
    from agent_framework import SupportsAgentRun

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

conditions = ["sunny", "cloudy", "raining", "snowing", "clear"]


@tool(approval_mode="never_require")
def get_weather(location: Annotated[str, "The city and state, e.g. San Francisco, CA"]) -> str:
    """Get the current weather for a given location."""
    condition = conditions[randrange(0, len(conditions))]  # noqa: S311
    temp = randrange(-10, 30)  # noqa: S311
    return f"The weather in {location} is {condition} and {temp}°C."


@tool(approval_mode="always_require")
def get_weather_detail(
    location: Annotated[str, "The city and state, e.g. San Francisco, CA"],
) -> str:
    """Get the current weather for a given location, with tomorrow's forecast."""
    today = conditions[randrange(0, len(conditions))]  # noqa: S311
    temp = randrange(-10, 30)  # noqa: S311
    tomorrow = conditions[randrange(0, len(conditions))]  # noqa: S311
    tomorrow_high = randrange(-10, 30)  # noqa: S311
    return (
        f"The weather in {location} is {today} and {temp}°C, with a humidity of 88%. "
        f"Tomorrow will be {tomorrow} with a high of {tomorrow_high}°C."
    )


async def handle_approvals(query: str, agent: SupportsAgentRun) -> AgentResponse:
    """When there's no thread, the original query, the approval request,
    and the approval response must all be included in each iteration."""
    result = await agent.run(query)
    while len(result.user_input_requests) > 0:
        new_inputs: list[Any] = [query]
        for user_input_needed in result.user_input_requests:
            if user_input_needed.function_call is None:
                continue
            print(
                f"\nUser Input Request for function from {agent.name}:"
                f"\n  Function: {user_input_needed.function_call.name}"
                f"\n  Arguments: {user_input_needed.function_call.arguments}"
            )
            new_inputs.append(Message("assistant", [user_input_needed]))
            user_approval = await asyncio.to_thread(input, "\nApprove function call? (y/n): ")
            new_inputs.append(
                Message(
                    "user",
                    [user_input_needed.to_function_approval_response(user_approval.lower() == "y")],
                )
            )
        result = await agent.run(new_inputs)
    return result


async def main() -> None:
    async with GovernedAgent(
        client=OpenAIChatCompletionClient(),
        name="WeatherAgent",
        instructions="You are a helpful weather assistant. Use the weather tools to answer.",
        tools=[get_weather, get_weather_detail],
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # see maf_sample_01/'s own module docstring
    ) as agent:
        query = "Can you give me an update of the weather in LA, and detailed weather for Seattle?"
        print(f"User: {query}")
        result = await handle_approvals(query, agent)
        print(f"\n{agent.name}: {result}\n")


if __name__ == "__main__":
    asyncio.run(main())
