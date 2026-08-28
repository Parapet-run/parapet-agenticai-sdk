"""SAME agent, SAME two identities, SAME task -- wrapped by Parapet.

Tony (Sales org) and Sally (HR org) share one agent with both tools
declared, exactly as in example_no_governance.py. The only difference is
the GovernedAgent import and one `governed_identity()` context manager per
call, asserting which org the caller is in. Cedar decides the rest: the
org policy pushed to this agent's bundle (policy/40-org.cedar, pushed via
parapet_push_policy_file when this demo was provisioned) permits
salesforce_lookup only for org=Sales and hr_lookup only for org=HR.

A denied tool call does not raise or crash the agent -- GovernedAgent
degrades gracefully, folding a synthetic denial into the conversation the
same way a real tool error would. This script does NOT infer ALLOW/DENY
by reading that text back (fragile) -- it captures Parapet's own Cedar
decision events via structlog, the same technique
examples/ungoverned_vs_governed/run.py uses, and reports the actual
decision.

REQUIRES an agent_id + agent_secret from a real Parapet control plane --
see .env.example. This is the one file in this demo that talks to the
control plane: every governed decision it makes is visible at
{control_plane_url}/agents/{agent_id} the moment it runs.

Model: mocked locally by default (mock_model_server.py). Set
OPENAI_API_KEY in .env to a real key to use a real model instead -- see
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
from mock_model_server import serve  # noqa: E402

SCENARIOS = [
    ("Tony", "Sales", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
    ("Tony", "Sales", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up my HR benefits and PTO balance", "hr_lookup"),
    ("Sally", "HR", "Look up the ACME account's Salesforce opportunity", "salesforce_lookup"),
]

_DECISIONS: list[dict] = []


def _capture(_logger: object, _method: str, event_dict: dict) -> dict:
    if event_dict.get("event") == "decision":
        _DECISIONS.append(dict(event_dict))
    return event_dict


structlog.configure(processors=[_capture, structlog.dev.ConsoleRenderer()])


def salesforce_lookup(query: str) -> str:
    """Look up a Salesforce opportunity by account name."""
    return f"Opportunity ACME-4471: $50k, stage=Negotiation (query: {query})"


def hr_lookup(query: str) -> str:
    """Look up an HR record: PTO balance, benefits enrollment."""
    return f"PTO balance: 12 days remaining (query: {query})"


def _real_model_configured() -> bool:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key) and key not in ("mock", "sk-mock", "changeme")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set -- example_governed.py needs a real provisioned agent "
            "(see .env.example). Run the parapet-quickdemo skill's provisioning step first."
        )
    return value


async def _run_one(
    client,
    name: str,
    org: str,
    prompt: str,
    agent_id: str,
    agent_secret: str,
    control_plane_url: str,
) -> tuple[str, str | None]:
    from parapetai_agent import GovernedAgent
    from parapetai_agent.scoped_data import governed_identity

    _DECISIONS.clear()
    async with GovernedAgent(
        client=client,
        name="workplace-agent",
        instructions=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
        tools=[salesforce_lookup, hr_lookup],
        agent_id=agent_id,
        agent_secret=agent_secret,
        control_plane_url=control_plane_url,
        console=False,
    ) as agent:
        with governed_identity(claims={"org": org, "name": name}):
            result = await agent.run(prompt)

    tool_decision = next((d for d in _DECISIONS if d.get("action") == "tool_call"), None)
    outcome = tool_decision.get("decision") if tool_decision else None
    return result.text, outcome


async def main() -> None:
    from agent_framework.openai import OpenAIChatCompletionClient

    agent_id = _required_env("PARAPETAI_AGENT_ID")
    agent_secret = _required_env("PARAPETAI_AGENT_SECRET")
    account_id = _required_env("PARAPETAI_ACCOUNT_ID")
    control_plane_url = os.environ.get("PARAPETAI_CONTROL_PLANE_URL", "https://app.parapet.run")

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
        text, outcome = await _run_one(
            client, name, org, prompt, agent_id, agent_secret, control_plane_url
        )
        label = "ALLOWED" if outcome == "allow" else "DENIED" if outcome == "deny" else "UNKNOWN"
        print(f"[governed] {name} ({org}) -> {expected_tool}: {label}")
        results.append(
            {
                "name": name,
                "org": org,
                "tool": expected_tool,
                "outcome": label,
                "text": text,
            }
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
