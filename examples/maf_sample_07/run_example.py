"""Live example: a governed agent with structured (Pydantic) output --
ported from Microsoft Agent Framework's own "OpenAI Chat Client with
Structured Output" sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/providers/openai/client_with_structured_output.py).

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story. policy_dir/entities_path/agent_id/control_plane_url/
agent_secret are all omitted -- bundled default Cedar policy (base
permits) and env-var fallbacks apply. This is the one sample in this
directory left on plain OpenAI (see .env.example) rather than Azure
OpenAI, to demonstrate the OTHER routing OpenAIChatCompletionClient()
auto-detects from environment alone -- same client class every other
maf_sample_0N/ uses, different env vars set.

Worth noting for this specific sample: `options={"response_format": ...}`
is a framework/model-level concern (how the model is asked to shape its
output), completely orthogonal to Cedar's model_call decision -- Cedar
governs WHETHER the call happens, never HOW the response is shaped.

Run (local dry run, governed by the bundled default policy, no control
plane needed):
    cp examples/maf_sample_07/.env.example examples/maf_sample_07/.env
    # fill in OPENAI_API_KEY in that .env
    uv run --with agent-framework python3 examples/maf_sample_07/run_example.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_framework import AgentResponse
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv
from pydantic import BaseModel

from parapetai_agent import GovernedAgent

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


class OutputStruct(BaseModel):
    """A structured output for testing purposes."""

    city: str
    description: str


def _make_agent() -> GovernedAgent:
    return GovernedAgent(
        client=OpenAIChatCompletionClient(),
        name="CityAgent",
        instructions="You are a helpful agent that describes cities in a structured format.",
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # see maf_sample_01/'s own module docstring
    )


async def non_streaming_example(agent: GovernedAgent) -> None:
    print("=== Non-streaming example ===")
    query = "Tell me about Paris, France"
    print(f"User: {query}")
    result = await agent.run(query, options={"response_format": OutputStruct})
    if structured_data := result.value:
        print("Structured Output Agent:")
        print(f"City: {structured_data.city}")
        print(f"Description: {structured_data.description}")
    else:
        print(f"Failed to parse response: {result.text}")


async def streaming_example(agent: GovernedAgent) -> None:
    print("=== Streaming example ===")
    query = "Tell me about Tokyo, Japan"
    print(f"User: {query}")
    result = await AgentResponse.from_update_generator(
        agent.run(query, stream=True, options={"response_format": OutputStruct}),
        output_format_type=OutputStruct,
    )
    if structured_data := result.value:
        print("Structured Output (from streaming):")
        print(f"City: {structured_data.city}")
        print(f"Description: {structured_data.description}")
    else:
        print(f"Failed to parse response: {result.text}")


async def main() -> None:
    async with _make_agent() as agent:
        await non_streaming_example(agent)
        await streaming_example(agent)


if __name__ == "__main__":
    asyncio.run(main())
