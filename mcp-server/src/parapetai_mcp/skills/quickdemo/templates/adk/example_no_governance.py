"""Baseline: a plain google.adk Agent + Runner, no Parapet involved at all.

Two identities that would exist in any real company -- Tony in Sales,
Sally in HR -- each ask the SAME agent to look up something in the OTHER
org's system. Nothing stops either of them: the agent has both tools
declared, the model decides to call whichever one matches the request,
and it just runs. This is the risk example_governed.py's identical setup
(minus the GovernedRunner swap) closes.

Model: mocked locally by default (mock_llm.py, a google.adk.models.BaseLlm
subclass -- no network call). Set GOOGLE_API_KEY in .env to a real key to
use a real Gemini model instead -- see .env.cloud.example /
.env.local.example. Either way the agent code below is unchanged; only
what's passed as model= differs. This script never reads PARAPETAI_MODE --
it never governs anything, so cloud vs. local mode is irrelevant to it.

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

from mock_llm import MockLlm  # noqa: E402

SCENARIOS = [
    ("Tony", "Sales", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
    ("Tony", "Sales", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
]

APP_NAME = "quickdemo"
MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.6-flash")


def salesforce_lookup(query: str) -> str:
    """Look up a Salesforce opportunity by account name."""
    return f"Opportunity ACME-4471: $50k, stage=Negotiation (query: {query})"


def hr_lookup(query: str) -> str:
    """Look up an HR record: PTO balance, benefits enrollment."""
    return f"PTO balance: 12 days remaining (query: {query})"


def _real_model_configured() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY", "").strip())


def _text_of(content) -> str:
    if content is None or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts)


def _usage_of(usage) -> dict[str, int | None]:
    # google.genai's GenerateContentResponseUsageMetadata -- the exact
    # attribute shape parapetai_agent.adk._token_count_attributes() reads
    # off LlmResponse.usage_metadata. Attribute access (not dict-like),
    # unlike agent_framework's UsageDetails TypedDict.
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    return {
        "prompt_tokens": usage.prompt_token_count,
        "completion_tokens": usage.candidates_token_count,
        "total_tokens": usage.total_token_count,
    }


async def _run_one(model, name: str, org: str, prompt: str) -> dict:
    from google.adk.agents import Agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    root_agent = Agent(
        name="workplace_agent",
        model=model,
        instruction=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
        tools=[salesforce_lookup, hr_lookup],
    )
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    session_id = f"s-{name.lower()}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=name, session_id=session_id
    )
    text = ""
    usage = None
    started = time.perf_counter()
    async for event in runner.run_async(
        user_id=name,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        t = _text_of(event.content)
        if t:
            text = t
        if event.usage_metadata is not None:
            usage = event.usage_metadata  # last non-None chunk carries the full total
    latency_ms = (time.perf_counter() - started) * 1000
    return {"text": text, "latency_ms": latency_ms, **_usage_of(usage)}


async def main() -> None:
    if _real_model_configured():
        mode = "real"
        model = MODEL  # a plain model-id string -- ADK resolves it via GOOGLE_API_KEY
    else:
        mode = "mock"
        model = MockLlm()

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        run = await _run_one(model, name, org, prompt)
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
