"""Live example: a governed agent with the framework's OWN, in-process
function-based middleware layered alongside Cedar governance -- ported
from Microsoft Agent Framework's own "Function-based Middleware" sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/middleware/function_based_middleware.py).

Uses the SAME client + credential upstream does -- FoundryChatClient and
the ASYNC `azure.identity.aio.AzureCliCredential` (this specific upstream
sample uses the async variant as an `async with` context manager,
unlike maf_sample_01/02/03/04's sync `azure.identity.AzureCliCredential`
-- both are real, both work, upstream just picked async here). Not a
simplified stand-in -- GovernedAgent is client-agnostic by construction
(see maf_sample_01/'s own module docstring for the full reasoning) -- no
parapetai-agent code needed changing to support this. See parapetai-support.yaml's
own `foundry` entry for exactly what's been verified vs. not.

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story. policy_dir/entities_path/agent_id/control_plane_url/
agent_secret are all omitted -- bundled default Cedar policy (base
permits) and env-var fallbacks apply.

Worth noting for this specific sample, the one thing GovernedAgent's own
docstring calls out explicitly: any middleware passed via `middleware=`
runs AFTER the governance middleware Cedar decides with, not instead of
it. `security_agent_middleware`/`logging_function_middleware` below are
this repo's own, ordinary agent_framework middleware -- GovernedAgent
prepends `[chat_mw, func_mw]` ahead of them in __init__, so Cedar always
gets first refusal on a call, and the sample's own middleware only ever
sees a call Cedar already permitted.

Run (local dry run, governed by the bundled default policy, no control
plane needed) -- needs `az login` once first (AzureCliCredential, same as
upstream, reads your Azure CLI's own local token cache -- not settable
via .env):
    az login
    cp examples/maf_sample_06/.env.example examples/maf_sample_06/.env
    # fill in FOUNDRY_PROJECT_ENDPOINT in that .env
    uv run --with agent-framework python3 examples/maf_sample_06/run_example.py
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from random import randint
from typing import Annotated

from agent_framework import AgentContext, FunctionInvocationContext, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import AzureCliCredential
from dotenv import load_dotenv
from pydantic import Field

from parapetai_agent import GovernedAgent

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


@tool(approval_mode="never_require")
def get_weather(
    location: Annotated[str, Field(description="The location to get the weather for.")],
) -> str:
    """Get the weather for a given location."""
    conditions = ["sunny", "cloudy", "rainy", "stormy"]
    condition = conditions[randint(0, 3)]  # noqa: S311
    high = randint(10, 30)  # noqa: S311
    return f"The weather in {location} is {condition} with a high of {high}°C."


async def security_agent_middleware(
    context: AgentContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Framework-level middleware -- Cedar has already decided by the
    time this runs (see this file's own module docstring)."""
    last_message = context.messages[-1] if context.messages else None
    if last_message and last_message.text:
        query = last_message.text
        if "password" in query.lower() or "secret" in query.lower():
            print("[SecurityAgentMiddleware] blocking request -- sensitive keyword detected.")
            return
    print("[SecurityAgentMiddleware] check passed.")
    await call_next()


async def logging_function_middleware(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    function_name = context.function.name
    print(f"[LoggingFunctionMiddleware] about to call: {function_name}.")
    start_time = time.time()
    await call_next()
    duration = time.time() - start_time
    print(f"[LoggingFunctionMiddleware] {function_name} completed in {duration:.5f}s.")


async def main() -> None:
    async with (
        AzureCliCredential() as credential,
        GovernedAgent(
            client=FoundryChatClient(credential=credential),
            name="WeatherAgent",
            instructions="You are a helpful weather assistant.",
            tools=[get_weather],
            middleware=[security_agent_middleware, logging_function_middleware],
            local_log_dir=EXAMPLE_DIR / "logs",
            console=False,  # see maf_sample_01/'s own module docstring
        ) as agent,
    ):
        print("\n--- Normal Query ---")
        query = "What's the weather like in Tokyo?"
        print(f"User: {query}")
        result = await agent.run(query)
        print(f"Agent: {result.text if result.text else 'No response'}\n")

        print("--- Security Test ---")
        query = "What's the secret weather password?"
        print(f"User: {query}")
        result = await agent.run(query)
        print(f"Agent: {result.text if result and result.text else 'No response'}\n")


if __name__ == "__main__":
    asyncio.run(main())
