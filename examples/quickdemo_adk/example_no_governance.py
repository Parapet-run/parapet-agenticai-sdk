"""Baseline: a plain google.adk Agent + Runner, no Parapet involved at all.

Two identities that would exist in any real company -- Tony in Sales,
Sally in HR -- each ask the SAME agent to look up something in the OTHER
org's system. Nothing stops either of them: the agent has both tools
declared, the model decides to call whichever one matches the request,
and it just runs. This is the risk example_governed.py's identical setup
(minus the GovernedRunner swap) closes.

Model: mocked locally by default (mock_llm.py, a google.adk.models.BaseLlm
subclass -- no network call). Set GOOGLE_API_KEY in .env to a real key to
use a real Gemini model instead -- see .env.example. Either way the agent
code below is unchanged; only what's passed as model= differs.

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


async def _run_one(model, name: str, org: str, prompt: str) -> str:
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
    async for event in runner.run_async(
        user_id=name,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        t = _text_of(event.content)
        if t:
            text = t
    return text


async def main() -> None:
    if _real_model_configured():
        mode = "real"
        model = MODEL  # a plain model-id string -- ADK resolves it via GOOGLE_API_KEY
    else:
        mode = "mock"
        model = MockLlm()

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        text = await _run_one(model, name, org, prompt)
        print(f"[ungoverned] {name} ({org}) -> {expected_tool}: {text}")
        results.append(
            {"name": name, "org": org, "tool": expected_tool, "outcome": "ALLOWED", "text": text}
        )

    print(
        "QUICKDEMO_RESULT_JSON:"
        + json.dumps(
            {
                "mode": mode,
                "results": results,
                "agent_id": os.environ.get("PARAPETAI_AGENT_ID_NOGOV") or None,
                "account_id": os.environ.get("PARAPETAI_ACCOUNT_ID") or None,
                "control_plane_url": os.environ.get(
                    "PARAPETAI_CONTROL_PLANE_URL", "https://app.parapet.run"
                ),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
