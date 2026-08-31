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
  read straight from ./policies/ on disk instead (GovernedRunner(policy_dir=
  "./policies")). Needs no agent_id/secret/account_id.

Model: mocked locally by default (mock_llm.py). Set GOOGLE_API_KEY in
.env to a real key to use a real Gemini model instead -- see
.env.cloud.example / .env.local.example.
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


def _text_of(content) -> str:
    if content is None or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts)


def _usage_of(usage) -> dict[str, int | None]:
    # Same google.genai UsageMetadata shape/extraction as
    # example_no_governance.py's _usage_of() -- see that file's comment.
    # GovernedRunner doesn't transform usage_metadata at all, so this reads
    # identically governed or not.
    if usage is None:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    return {
        "prompt_tokens": usage.prompt_token_count,
        "completion_tokens": usage.candidates_token_count,
        "total_tokens": usage.total_token_count,
    }


async def _run_one(
    model,
    name: str,
    org: str,
    prompt: str,
    *,
    agent_id: str | None,
    agent_secret: str | None,
    control_plane_url: str | None,
    policy_dir: str | None,
    persist_policy_dir: str | None,
) -> dict:
    from google.adk.agents import Agent
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from parapetai_agent.adk import GovernedRunner, governed_identity

    governed_kwargs: dict = {"console": False}
    if policy_dir is not None:
        governed_kwargs["policy_dir"] = policy_dir  # PARAPETAI_MODE=local
    else:
        governed_kwargs.update(  # PARAPETAI_MODE=cloud
            agent_id=agent_id,
            agent_secret=agent_secret,
            control_plane_url=control_plane_url,
            persist_policy_dir=persist_policy_dir,
        )

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
        **governed_kwargs,
    )
    session_id = f"s-{name.lower()}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=name, session_id=session_id
    )

    text = ""
    usage = None
    started = time.perf_counter()
    with governed_identity(claims={"org": org, "name": name}):
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
    latency_ms = (time.perf_counter() - started) * 1000  # model + tool + governance, wall-clock

    # The Cedar Decision is the SAME dataclass (policy/engine.py's Decision,
    # via to_audit_record()) regardless of framework -- effect, reason,
    # determining_policies, evaluation_ms are all present here exactly as
    # they'd be from bare Governor or GovernedAgent. Only the SURFACE that
    # folds a deny into the agent's own response differs by framework (ADK
    # uses a synthetic LlmResponse/dict, never an exception -- see adk.py's
    # own module docstring); the decision data itself is not
    # framework-specific.
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
        **_usage_of(usage),
    }


async def main() -> None:
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

    if _real_model_configured():
        mode = "real"
        model = MODEL
    else:
        mode = "mock"
        model = MockLlm()

    results = []
    for name, org, prompt, expected_tool in SCENARIOS:
        run = await _run_one(
            model,
            name,
            org,
            prompt,
            agent_id=agent_id,
            agent_secret=agent_secret,
            control_plane_url=control_plane_url,
            policy_dir=policy_dir,
            persist_policy_dir=persist_policy_dir,
        )
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
    asyncio.run(main())
    sys.exit(0)
