"""Baseline: a plain agent_framework.Agent, no Parapet involved at all.

Two identities that would exist in any real company -- Tony in Sales,
Sally in HR -- each ask the SAME agent to look up something in the OTHER
org's system. Nothing stops either of them: the agent has both tools
declared, the model decides to call whichever one matches the request,
and it just runs. This is the risk example_governed.py's identical setup
(minus the GovernedAgent swap) closes.

Model: mocked locally by default (mock_model_server.py, stdlib only, no
network). Set OPENAI_API_KEY in .env to a real key to use a real model
instead -- see .env.cloud.example / .env.local.example. Either way the
agent code below is unchanged; only where OpenAIChatCompletionClient
points differs. This script never reads PARAPETAI_MODE -- it never
governs anything, so cloud vs. local mode is irrelevant to it.

Run directly for readable output, or via driver.py for the side-by-side
comparison with example_governed.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
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


def _usage_of(result) -> dict[str, int | None]:
    # agent_framework's AgentResponse.usage_details is a UsageDetails
    # TypedDict (input_token_count/output_token_count/total_token_count) --
    # the exact same shape parapetai_agent.maf._token_count_attributes()
    # reads off ChatResponse.usage_details. getattr()'d defensively since
    # a mock/older client may not populate it.
    usage = getattr(result, "usage_details", None) or {}
    return {
        "prompt_tokens": usage.get("input_token_count"),
        "completion_tokens": usage.get("output_token_count"),
        "total_tokens": usage.get("total_token_count"),
    }


async def _run_one(client, name: str, org: str, prompt: str) -> dict:
    from agent_framework import Agent

    started = time.perf_counter()
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
    latency_ms = (time.perf_counter() - started) * 1000
    return {"text": result.text, "latency_ms": latency_ms, **_usage_of(result)}


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
        run = await _run_one(client, name, org, prompt)
        tokens_note = run["total_tokens"] if run["total_tokens"] is not None else "-"
        print(
            f"[ungoverned] {name} ({org}) -> {expected_tool}: {run['text']} "
            f"({run['latency_ms']:.1f}ms, {tokens_note} tokens)"
        )
        results.append(
            {
                "name": name,
                "org": org,
                "tool": expected_tool,
                "outcome": "ALLOWED",
                "text": run["text"],
                "latency_ms": run["latency_ms"],
                "prompt_tokens": run["prompt_tokens"],
                "completion_tokens": run["completion_tokens"],
                "total_tokens": run["total_tokens"],
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
