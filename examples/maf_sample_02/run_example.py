"""Live example: a governed agent with a function tool -- ported from
Microsoft Agent Framework's own "Add Tools" sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/02_add_tools.py).

Uses the SAME client + credential upstream does -- FoundryChatClient and
AzureCliCredential -- not a simplified stand-in. GovernedAgent doesn't
care which agent_framework-compatible client backs it (see
maf_sample_01/'s own module docstring for the full reasoning) -- no
parapetai-agent code needed changing to support this.

WIRING is the same minimal shape every maf_sample_0N/ uses now -- see
maf_sample_01/'s own module docstring / docs/maf-integration-pattern.md
for the full story:

    agent = GovernedAgent(
        client=..., name=..., instructions=..., tools=[...],
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,
    )

policy_dir/entities_path/agent_id/control_plane_url/agent_secret are all
omitted -- bundled default Cedar policy (base permits) and env-var
fallbacks apply, exactly as in maf_sample_01/. Set
PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET/PARAPETAI_AGENT_ID in .env to
govern this by a real control-plane-provisioned agent's bundle instead.
console=False: local_log_dir still writes the audit log file, this just
skips ALSO echoing it (and, if a control plane is configured, the
auto-wired OTel console exporter) to stdout -- see maf_sample_01/'s own
module docstring for the full story.

Worth noting for this specific sample: `@tool(...)`-decorated functions
are governed exactly like any other tool call -- Cedar's `tool_call`
action (the bundled policy's own base permit, or a tighter policy
narrowing which agents/tools combinations are allowed) evaluates
`get_weather` the same way it would evaluate an MCP tool call or any
other function tool. Upstream's own `approval_mode` is a
framework-level gate that runs independently of and alongside Cedar's
decision -- see maf_sample_05/ for the approval-required,
human-in-the-loop shape.

Run (local dry run, governed by the bundled default policy, no control
plane needed) -- needs `az login` once first (AzureCliCredential, same as
upstream, reads your Azure CLI's own local token cache -- not settable
via .env):
    az login
    cp examples/maf_sample_02/.env.example examples/maf_sample_02/.env
    # fill in FOUNDRY_PROJECT_ENDPOINT in that .env
    uv run --with agent-framework python3 examples/maf_sample_02/run_example.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from random import randint
from typing import Annotated

from agent_framework import tool
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from pydantic import Field

from parapetai_agent import GovernedAgent

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


# NOTE: approval_mode="never_require" is for sample brevity, same caveat
# upstream's own sample carries -- see maf_sample_05/ for the
# approval-required, human-in-the-loop shape.
@tool(approval_mode="never_require")
def get_weather(
    location: Annotated[str, Field(description="The location to get the weather for.")],
) -> str:
    """Get the weather for a given location."""
    # Cosmetic sample data only, never a security-relevant value.
    conditions = ["sunny", "cloudy", "rainy", "stormy"]
    condition = conditions[randint(0, 3)]  # noqa: S311
    high = randint(10, 30)  # noqa: S311
    return f"The weather in {location} is {condition} with a high of {high}°C."


async def main() -> None:
    async with GovernedAgent(
        client=FoundryChatClient(credential=AzureCliCredential()),
        name="WeatherAgent",
        instructions="You are a helpful weather agent. Use the get_weather tool to answer.",
        tools=[get_weather],
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,
    ) as agent:
        result = await agent.run("What's the weather like in Seattle?")
        print(f"Agent: {result}")


if __name__ == "__main__":
    asyncio.run(main())
