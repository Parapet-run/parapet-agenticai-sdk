"""Baseline: a plain agent_framework.Agent, no Parapet involved at all.

Two identities that would exist in any real company -- Tony in Sales,
Sally in HR -- each ask the SAME agent to look up something in the OTHER
org's system. Nothing stops either of them: the agent has both tools
declared, the model decides to call whichever one matches the request,
and it just runs. This is the risk example_governed.py's identical setup
(minus the GovernedAgent swap) closes.

Model: mocked locally by default (mock_model_server.py, stdlib only, no
network). Set OPENAI_API_KEY in .env to a real key to use a real model
instead -- see .env.example. Either way the agent code below is
unchanged; only where OpenAIChatCompletionClient points differs.

Run directly for readable output, or via driver.py for the side-by-side
comparison with example_governed.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from mock_model_server import serve  # noqa: E402

SCENARIOS = [
    ("Tony", "Sales", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
    ("Tony", "Sales", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
]


def salesforce_lookup(query: str) -> str:
    """Look up a Salesforce opportunity by account name."""
    return f"Opportunity ACME-4471: $50k, stage=Negotiation (query: {query})"


def hr_lookup(query: str) -> str:
    """Look up an HR record: PTO balance, benefits enrollment."""
    return f"PTO balance: 12 days remaining (query: {query})"


def _real_model_configured() -> bool:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key) and key not in ("mock", "sk-mock", "changeme")


async def _run_one(client, name: str, org: str, prompt: str) -> str:
    from agent_framework import Agent

    async with Agent(
        client=client,
        name="workplace-agent",
        instructions=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
        tools=[salesforce_lookup, hr_lookup],
    ) as agent:
        result = await agent.run(prompt)
        return result.text


async def main() -> None:
    from agent_framework.openai import OpenAIChatCompletionClient

    if _real_model_configured():
        mode = "real"
        client_kwargs: dict = {"model": os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")}
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
    else:
        mode = "mock"
        server = serve()
        client_kwargs = {
            "model": "mock-model",
            "api_key": "mock",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        }

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        client = OpenAIChatCompletionClient(**client_kwargs)
        text = await _run_one(client, name, org, prompt)
        print(f"[ungoverned] {name} ({org}) -> {expected_tool}: {text}")
        results.append(
            {
                "name": name,
                "org": org,
                "tool": expected_tool,
                "outcome": "ALLOWED",
                "text": text,
            }
        )

    # No agent_id/control_plane_url here -- this script never calls the
    # control plane at all, unlike example_governed.py. One provisioned
    # agent (the governed one) is enough for this demo; a second one
    # purely to have a page to point at would never show anything on it.
    print("QUICKDEMO_RESULT_JSON:" + json.dumps({"mode": mode, "results": results}))


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
