"""SAME agent, SAME two identities, SAME task -- wrapped by Parapet.

Tony (Sales org) and Sally (HR org) share one agent with both tools
declared, exactly as in example_no_governance.py. The only difference is
the ParapetAgentMiddleware import and one `governed_identity()` context
manager per call, asserting which org the caller is in -- the SAME context
manager parapetai_agent.maf's/parapetai_agent.adk's own quickdemo examples
use, re-exported unchanged from parapetai_agent.scoped_data. Cedar decides
the rest: the org policy pushed to this agent's bundle (policy/40-org.cedar,
pushed via parapet_push_policy_file when this demo was provisioned) permits
salesforce_lookup only for org=Sales and hr_lookup only for org=HR.

A denied tool (or model) call DOES raise here, unlike the MAF/ADK versions
of this same demo -- ParapetAgentMiddleware.wrap_tool_call/wrap_model_call
raise GovernanceDenied before langchain.agents.create_agent's own handler
ever runs (see parapetai_agent/langgraph.py's own module docstring: this is
a verified, genuine block, not a synthetic denial folded into the
conversation the way MAF's/ADK's middleware degrade). This script still
uses the SAME structlog "decision" event capture technique the MAF/ADK
quickdemos use (every framework logs through the same
governance_runtime.audit() sink regardless of how a deny surfaces, so this
captures determining_policies/evaluation_ms identically for both an ALLOW
and a DENY row) -- it additionally catches GovernanceDenied to know the
call was blocked and to get the agent's own text out of the exception,
since a raised exception means there is no final AIMessage to read that
from.

Two modes, toggled by PARAPETAI_MODE in .env (see .env.cloud.example /
.env.local.example, and README.md's "Switching between cloud and local
mode"):

- PARAPETAI_MODE=cloud (default) -- REQUIRES an agent_id + agent_secret
  from a real Parapet control plane. This is the one file in this demo
  that talks to the control plane in this mode: every governed decision
  it makes is visible at {control_plane_url}/a/{account_id}/agents/{agent_id}
  the moment it runs. The fetched bundle is also written to
  PARAPETAI_PERSIST_POLICY_DIR (on by default) so you can inspect what the
  control plane actually sent.
- PARAPETAI_MODE=local -- no control-plane call at all. Cedar policies are
  read straight from ./policies/ on disk instead (build_middleware(policy_dir=
  "./policies")). Needs no agent_id/secret/account_id.

Model: mocked locally by default (mock_model_server.py). Set
OPENAI_API_KEY in .env to a real key to use a real model instead -- see
.env.cloud.example / .env.local.example.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import structlog  # noqa: E402
from mock_model_server import serve  # noqa: E402

_DECISIONS: list[dict] = []


def _capture(_logger: object, _method: str, event_dict: dict) -> dict:
    if event_dict.get("event") == "decision":
        _DECISIONS.append(dict(event_dict))
    return event_dict


structlog.configure(processors=[_capture, structlog.dev.ConsoleRenderer()])

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


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set -- example_governed.py needs a real provisioned agent "
            "in cloud mode (see .env.cloud.example). Run the parapet-quickdemo skill's "
            "provisioning step first, or set PARAPETAI_MODE=local to skip the control "
            "plane entirely (see .env.local.example)."
        )
    return value


def _resolve_persist_policy_dir() -> str | None:
    # On by default -- a disposable local cache of the fetched control-plane
    # bundle, useful for diffing against ./policies/40-org.cedar when a
    # decision looks wrong. Never bundle/deploy it; see README.md.
    raw = os.environ.get("PARAPETAI_PERSIST_POLICY_DIR")
    if raw is None:
        return str(Path(__file__).parent / ".parapet-cache" / "policies")
    raw = raw.strip()
    if not raw:
        return None
    path = Path(raw)
    return str(path if path.is_absolute() else Path(__file__).parent / path)


def _usage_of(result) -> dict[str, int | None]:
    # Same UsageMetadata shape/extraction as example_no_governance.py's
    # _usage_of() -- see that file's comment. ParapetAgentMiddleware doesn't
    # transform usage_metadata at all, so this reads identically governed
    # or not.
    usage = getattr(result, "usage_metadata", None) or {}
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _run_one(
    model,
    name: str,
    org: str,
    prompt: str,
    *,
    middleware,
) -> dict:
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    from parapetai_agent import GovernanceDenied
    from parapetai_agent.scoped_data import governed_identity

    agent = create_agent(
        model,
        tools=[tool(salesforce_lookup), tool(hr_lookup)],
        system_prompt=(
            "You are a workplace assistant with access to internal tools. "
            "Use the tool that matches what the user is asking for."
        ),
        middleware=[middleware],
    )

    _DECISIONS.clear()
    started = time.perf_counter()
    try:
        with governed_identity(claims={"org": org, "name": name}):
            result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
        latency_ms = (time.perf_counter() - started) * 1000  # model + tool + governance, wall-clock
        final = result["messages"][-1]
        text: str | None = final.content
        usage = _usage_of(final)
    except GovernanceDenied as denied:
        # ParapetAgentMiddleware raises a real exception on deny (see this
        # file's own module docstring) -- MAF/ADK instead fold a deny into
        # a synthetic response and never raise. Either way the Cedar
        # Decision is the SAME dataclass (policy/engine.py's Decision);
        # only the SURFACE differs by framework. denied.decision itself
        # already carries everything below, but this reads it back off the
        # captured structlog event instead, for the exact same reason the
        # MAF/ADK quickdemos do: one shared code path below handles both
        # ALLOW and DENY rows, rather than two different data sources.
        latency_ms = (time.perf_counter() - started) * 1000
        text = denied.decision.reason
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    # The Cedar Decision reaches structlog the SAME way regardless of
    # framework (governance_runtime.audit(), the same sink MAF/ADK use) --
    # so this captures determining_policies/evaluation_ms identically for
    # an ALLOW or a DENY row, unlike reading them off the exception alone
    # (which only exists for a DENY).
    tool_decision = next((d for d in _DECISIONS if d.get("action") == "tool_call"), None)
    outcome = tool_decision.get("decision") if tool_decision else None
    determining_policies = (
        list(tool_decision.get("determining_policies") or []) if tool_decision else []
    )
    evaluation_ms = tool_decision.get("evaluation_ms") if tool_decision else None
    return {
        "text": text,
        "outcome": outcome,
        "determining_policies": determining_policies,
        "evaluation_ms": evaluation_ms,
        "latency_ms": latency_ms,
        **usage,
    }


def main() -> None:
    from langchain_openai import ChatOpenAI

    from parapetai_agent.langgraph import build_middleware

    governance_mode = os.environ.get("PARAPETAI_MODE", "cloud").strip().lower()
    if governance_mode not in ("cloud", "local"):
        raise SystemExit(f"PARAPETAI_MODE must be 'cloud' or 'local', got {governance_mode!r}")

    if governance_mode == "local":
        agent_id = os.environ.get("PARAPETAI_AGENT_ID", "").strip() or "local-quickdemo"
        agent_secret = None
        account_id = os.environ.get("PARAPETAI_ACCOUNT_ID", "").strip()
        control_plane_url = ""
        policy_dir: str | None = str(Path(__file__).parent / "policies")
        persist_policy_dir = None
    else:
        agent_id = _required_env("PARAPETAI_AGENT_ID")
        agent_secret = _required_env("PARAPETAI_AGENT_SECRET")
        account_id = _required_env("PARAPETAI_ACCOUNT_ID")
        control_plane_url = os.environ.get(
            "PARAPETAI_CONTROL_PLANE_URL", "https://app.parapet.run"
        )
        policy_dir = None
        persist_policy_dir = _resolve_persist_policy_dir()

    build_kwargs: dict = {"agent_id": agent_id, "console": False}
    if policy_dir is not None:
        build_kwargs["policy_dir"] = policy_dir  # PARAPETAI_MODE=local
    else:
        build_kwargs.update(  # PARAPETAI_MODE=cloud
            agent_secret=agent_secret,
            control_plane_url=control_plane_url,
            persist_policy_dir=persist_policy_dir,
        )
    middleware = build_middleware(**build_kwargs)

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
        run = _run_one(model, name, org, prompt, middleware=middleware)
        outcome = run["outcome"]
        label = "ALLOWED" if outcome == "allow" else "DENIED" if outcome == "deny" else "UNKNOWN"
        policy_note = ",".join(run["determining_policies"]) or "-"
        eval_note = f"{run['evaluation_ms']:.3f}ms" if run["evaluation_ms"] is not None else "-"
        tokens_note = run["total_tokens"] if run["total_tokens"] is not None else "-"
        print(
            f"[governed/{governance_mode}] {name} ({org}) -> {expected_tool}: {label} "
            f"(policy: {policy_note}, cedar eval: {eval_note}, "
            f"total: {run['latency_ms']:.1f}ms, {tokens_note} tokens)"
        )
        results.append(
            {
                "name": name,
                "org": org,
                "tool": expected_tool,
                "outcome": label,
                "text": run["text"],
                "determining_policies": run["determining_policies"],
                "evaluation_ms": run["evaluation_ms"],
                "latency_ms": run["latency_ms"],
                "prompt_tokens": run["prompt_tokens"],
                "completion_tokens": run["completion_tokens"],
                "total_tokens": run["total_tokens"],
            }
        )

    print(
        "QUICKDEMO_RESULT_JSON:"
        + json.dumps(
            {
                "mode": mode,
                "governance_mode": governance_mode,
                "agent_id": agent_id,
                "account_id": account_id,
                "control_plane_url": control_plane_url,
                "policy_dir": policy_dir,
                "results": results,
            }
        )
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
