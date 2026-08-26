"""Live example: a governed agent with multi-turn conversation state --
ported from Microsoft Agent Framework's own "Multi-Turn Conversations"
sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/03_multi_turn.py).

Uses the SAME client + credential upstream does -- FoundryChatClient and
AzureCliCredential -- not a simplified stand-in. GovernedAgent is
client-agnostic by construction (see maf_sample_01/'s own module
docstring for the full reasoning). See parapetai-support.yaml's own
`foundry` entry for exactly what's been verified vs. not.

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story. policy_dir/entities_path/agent_id/control_plane_url/
agent_secret are all omitted -- bundled default Cedar policy (base
permits) and env-var fallbacks apply.

Worth noting for this specific sample: AgentSession is entirely a
framework-level concept (conversation history), orthogonal to Cedar
governance -- every turn on the SAME session is still its own independent
model_call decision, evaluated fresh each time, same as any other
GovernedAgent.run() call. Session state carrying context across turns
doesn't carry an "already decided" status across turns.

Run (local dry run, governed by the bundled default policy, no control
plane needed) -- needs `az login` once first (AzureCliCredential, same as
upstream, reads your Azure CLI's own local token cache -- not settable
via .env):
    az login
    cp examples/maf_sample_03/.env.example examples/maf_sample_03/.env
    # fill in FOUNDRY_PROJECT_ENDPOINT in that .env
    uv run --with agent-framework python3 examples/maf_sample_03/run_example.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from parapetai_agent import GovernedAgent

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


async def main() -> None:
    async with GovernedAgent(
        client=FoundryChatClient(credential=AzureCliCredential()),
        name="ConversationAgent",
        instructions="You are a friendly assistant. Keep your answers brief.",
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # see maf_sample_01/'s own module docstring
    ) as agent:
        # Create a session to maintain conversation history
        session = agent.create_session()

        # First turn
        result = await agent.run("My name is Alice and I love hiking.", session=session)
        print(f"Agent: {result}\n")

        # Second turn -- the agent should remember the user's name and hobby
        result = await agent.run("What do you remember about me?", session=session)
        print(f"Agent: {result}")


if __name__ == "__main__":
    asyncio.run(main())
