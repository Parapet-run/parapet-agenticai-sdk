"""Live example: a governed agent with a memory/context provider --
ported from Microsoft Agent Framework's own "Agent Memory with Context
Providers and Session State" sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/04_memory.py).

Uses the SAME client + credential upstream does -- FoundryChatClient and
AzureCliCredential -- not a simplified stand-in. GovernedAgent is
client-agnostic by construction (see maf_sample_01/'s own module
docstring for the full reasoning) -- no parapetai-agent code needed changing to
support this. See parapetai-support.yaml's own `foundry` entry for exactly
what's been verified vs. not.

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story. policy_dir/entities_path/agent_id/control_plane_url/
agent_secret are all omitted -- bundled default Cedar policy (base
permits) and env-var fallbacks apply.

Worth noting for this specific sample: `context_providers=[...]` passes
straight through GovernedAgent's `**kwargs` to `agent_framework.Agent`
unchanged -- GovernedAgent only intercepts `policy_dir`/`entities_path`/
`agent_id`/`tenant`/`control_plane_url`/`agent_secret`/`pep_key_path`/
`local_log_dir`/..., everything else (tools, context_providers,
middleware, ...) passes through exactly as it would to a plain Agent(...)
call.

Run (local dry run, governed by the bundled default policy, no control
plane needed) -- needs `az login` once first (AzureCliCredential, same as
upstream, reads your Azure CLI's own local token cache -- not settable
via .env):
    az login
    cp examples/maf_sample_04/.env.example examples/maf_sample_04/.env
    # fill in FOUNDRY_PROJECT_ENDPOINT in that .env
    uv run --with agent-framework python3 examples/maf_sample_04/run_example.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_framework import AgentSession, ContextProvider, SessionContext
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from parapetai_agent import GovernedAgent

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


class UserMemoryProvider(ContextProvider):
    """A context provider that remembers user info in session state."""

    DEFAULT_SOURCE_ID = "user_memory"

    def __init__(self) -> None:
        super().__init__(self.DEFAULT_SOURCE_ID)

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession | None,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Inject personalization instructions based on stored user info."""
        user_name = state.get("user_name")
        if user_name:
            context.extend_instructions(
                self.source_id,
                f"The user's name is {user_name}. Always address them by name.",
            )
        else:
            context.extend_instructions(
                self.source_id,
                "You don't know the user's name yet. Ask for it politely.",
            )

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession | None,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Extract and store user info in session state after each call."""
        for msg in context.input_messages:
            text = msg.text if hasattr(msg, "text") else ""
            if isinstance(text, str) and "my name is" in text.lower():
                name = text.lower().split("my name is")[-1].strip().split()[0]
                state["user_name"] = name.capitalize()


async def main() -> None:
    async with GovernedAgent(
        client=FoundryChatClient(credential=AzureCliCredential()),
        name="MemoryAgent",
        instructions="You are a friendly assistant.",
        context_providers=[UserMemoryProvider()],
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # see maf_sample_01/'s own module docstring
    ) as agent:
        session = agent.create_session()

        # The provider doesn't know the user yet -- it will ask for a name
        result = await agent.run("Hello! What's the square root of 9?", session=session)
        print(f"Agent: {result}\n")

        # Now provide the name -- the provider stores it in session state
        result = await agent.run("My name is Alice", session=session)
        print(f"Agent: {result}\n")

        # Subsequent calls are personalized -- name persists via session state
        result = await agent.run("What is 2 + 2?", session=session)
        print(f"Agent: {result}\n")

        # Inspect session state to see what the provider stored
        provider_state = session.state.get("user_memory", {})
        print(f"[Session State] Stored user name: {provider_state.get('user_name')}")


if __name__ == "__main__":
    asyncio.run(main())
