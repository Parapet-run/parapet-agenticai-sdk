"""Baseline: a plain langchain.agents.create_agent, no Parapet involved at
all.

Two identities that would exist in any real company -- Tony in Sales,
Sally in HR -- each ask the SAME agent to look up something in the OTHER
org's system. Nothing stops either of them: the agent has both tools
declared, the model decides to call whichever one matches the request,
and it just runs. This is the risk example_governed.py's identical setup
(minus the middleware=[...] swap) closes.

Model: mocked locally by default (mock_model_server.py, stdlib only, no
network) -- an OpenAI-compatible HTTP stand-in, talked to via
langchain_openai.ChatOpenAI (not a parapetai_agent[langgraph] dependency
itself; see pyproject.toml). Set OPENAI_API_KEY in .env to a real key to
use a real model instead -- see .env.cloud.example / .env.local.example.
Either way the agent code below is unchanged; only where ChatOpenAI points
differs. This script never reads PARAPETAI_MODE -- it never governs
anything, so cloud vs. local mode is irrelevant to it.

Run directly for readable output, or via driver.py for the side-by-side
comparison with example_governed.py.
"""

from __future__ import annotations

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
    # The final AIMessage's usage_metadata is a langchain_core UsageMetadata
    # TypedDict (input_tokens/output_tokens/total_tokens) -- the same shape
    # example_governed.py reads, since ParapetAgentMiddleware doesn't
    # transform it at all.
    usage = getattr(result, "usage_metadata", None) or {}
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _agent_for(model):
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    return create_agent(
        model,
        tools=[tool(salesforce_lookup), tool(hr_lookup)],
        system_prompt=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
    )


def _run_one(agent, prompt: str) -> dict:
    started = time.perf_counter()
    result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    latency_ms = (time.perf_counter() - started) * 1000
    final = result["messages"][-1]
    return {"text": final.content, "latency_ms": latency_ms, **_usage_of(final)}


def main() -> None:
    from langchain_openai import ChatOpenAI

    if _real_model_configured():
        mode = "real"
        model_kwargs: dict = {"model": os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")}
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            model_kwargs["base_url"] = base_url
    else:
        mode = "mock"
        server = serve()
        model_kwargs = {
            "model": "mock-model",
            "api_key": "mock",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        }

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        model = ChatOpenAI(**model_kwargs)
        agent = _agent_for(model)
        run = _run_one(agent, prompt)
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
    main()
    sys.exit(0)
