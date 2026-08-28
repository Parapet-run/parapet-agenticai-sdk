"""SAME agent, SAME two identities, SAME task -- wrapped by Parapet.

Tony (Sales org) and Sally (HR org) share one agent with both tools
declared, exactly as in example_no_governance.py. The only difference is
the GovernedRunner import and one `governed_identity()` context manager
per call, asserting which org the caller is in -- the SAME context
manager parapetai_agent.maf's example uses, re-exported unchanged from
parapetai_agent.scoped_data (see parapetai_agent/adk.py's own import).
Cedar decides the rest: the org policy pushed to this agent's bundle
(policy/40-org.cedar, pushed via parapet_push_policy_file when this demo
was provisioned) permits salesforce_lookup only for org=Sales and
hr_lookup only for org=HR.

A denied tool call does not raise or crash the agent -- GovernedRunner
degrades gracefully, folding a synthetic denial into the conversation the
same way a real tool error would. This script does NOT infer ALLOW/DENY
by reading that text back (fragile) -- it captures Parapet's own Cedar
decision events via structlog, the same technique
examples/ungoverned_vs_governed/run.py uses, and reports the actual
decision.

REQUIRES an agent_id + agent_secret from a real Parapet control plane --
see .env.example. This is the one file in this demo that talks to the
control plane: every governed decision it makes is visible at
{control_plane_url}/a/{account_id}/agents/{agent_id} the moment it runs.

Model: mocked locally by default (mock_llm.py). Set GOOGLE_API_KEY in
.env to a real key to use a real Gemini model instead -- see
.env.example.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import structlog  # noqa: E402
from mock_llm import MockLlm  # noqa: E402

SCENARIOS = [
    ("Tony", "Sales", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
    ("Tony", "Sales", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
]

APP_NAME = "quickdemo"
MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.6-flash")

_DECISIONS: list[dict] = []


def _capture(_logger: object, _method: str, event_dict: dict) -> dict:
    if event_dict.get("event") == "decision":
        _DECISIONS.append(dict(event_dict))
    raise structlog.DropEvent


structlog.configure(processors=[_capture])


def salesforce_lookup(query: str) -> str:
    """Look up a Salesforce opportunity by account name."""
    return f"Opportunity ACME-4471: $50k, stage=Negotiation (query: {query})"


def hr_lookup(query: str) -> str:
    """Look up an HR record: PTO balance, benefits enrollment."""
    return f"PTO balance: 12 days remaining (query: {query})"


def _real_model_configured() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY", "").strip())


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set -- example_governed.py needs a real provisioned agent "
            "(see .env.example). Run the parapet-quickdemo skill's provisioning step first."
        )
    return value


def _text_of(content) -> str:
    if content is None or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts)


async def _run_one(
    model,
    name: str,
    org: str,
    prompt: str,
    agent_id: str,
    agent_secret: str,
    control_plane_url: str,
) -> tuple[str, str | None]:
    from google.adk.agents import Agent
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from parapetai_agent.adk import GovernedRunner, governed_identity

    _DECISIONS.clear()
    root_agent = Agent(
        name="workplace_agent",
        model=model,
        instruction=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
        tools=[salesforce_lookup, hr_lookup],
    )
    runner = GovernedRunner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=InMemorySessionService(),
        agent_id=agent_id,
        agent_secret=agent_secret,
        control_plane_url=control_plane_url,
        console=False,
    )
    session_id = f"s-{name.lower()}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=name, session_id=session_id
    )

    text = ""
    with governed_identity(claims={"org": org, "name": name}):
        async for event in runner.run_async(
            user_id=name,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            t = _text_of(event.content)
            if t:
                text = t

    tool_decision = next((d for d in _DECISIONS if d.get("action") == "tool_call"), None)
    outcome = tool_decision.get("decision") if tool_decision else None
    return text, outcome


async def main() -> None:
    agent_id = _required_env("PARAPETAI_AGENT_ID")
    agent_secret = _required_env("PARAPETAI_AGENT_SECRET")
    account_id = _required_env("PARAPETAI_ACCOUNT_ID")
    control_plane_url = os.environ.get("PARAPETAI_CONTROL_PLANE_URL", "https://app.parapet.run")

    if _real_model_configured():
        mode = "real"
        model = MODEL
    else:
        mode = "mock"
        model = MockLlm()

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        text, outcome = await _run_one(
            model, name, org, prompt, agent_id, agent_secret, control_plane_url
        )
        label = "ALLOWED" if outcome == "allow" else "DENIED" if outcome == "deny" else "UNKNOWN"
        print(f"[governed] {name} ({org}) -> {expected_tool}: {label}")
        results.append(
            {"name": name, "org": org, "tool": expected_tool, "outcome": label, "text": text}
        )

    print(
        "QUICKDEMO_RESULT_JSON:"
        + json.dumps(
            {
                "mode": mode,
                "agent_id": agent_id,
                "account_id": account_id,
                "control_plane_url": control_plane_url,
                "results": results,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
