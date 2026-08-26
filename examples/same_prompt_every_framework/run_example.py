"""The same two prompts, the same Cedar rule, five agent frameworks.

The point is the LAST column. The integration line differs per framework --
each puts its governable seam somewhere different -- but the decision does
not: `delete_incident` never executes, in any of them, because one Cedar rule
in `policies/10-incident.cedar` forbids it.

Frameworks that are not installed are skipped, not failed. You install the one
you use; nobody installs all five.

    export ANTHROPIC_API_KEY=sk-ant-...
    uv run --extra maf --extra adk --extra judge python3 \
        examples/same_prompt_every_framework/run_example.py

Add the rest to widen the table:
    pip install crewai langgraph langchain-anthropic openai-agents \
        agent-framework-anthropic
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import adk, crewai, langgraph, maf, openai_agents  # noqa: E402
from adapters._shared import PROMPT_ALLOW, PROMPT_DENY  # noqa: E402

from parapetai_agent import GovernanceDenied  # noqa: E402

ADAPTERS = [maf, adk, openai_agents, crewai, langgraph]

# CAPTURE the decision stream rather than reading exceptions.
#
# Exceptions are not a reliable signal here and that is the whole difficulty:
# MAF raises GovernanceDenied but its own framework swallows it before the
# caller sees it (see maf.py's "Enforcement asymmetry"), and ADK never raises
# at all -- it replaces the tool result. Inferring "blocked" from "no exception
# and the body did not run" cannot tell a block apart from a model that simply
# declined to call the tool.
#
# Every integration emits the same structlog "decision" event, so that is the
# one signal common to all five. Same technique as ungoverned_vs_governed/.
logging.getLogger().setLevel(logging.WARNING)

DECISIONS: list[dict] = []


def _capture(_logger, _method, event_dict):  # noqa: ANN001, ANN202
    if event_dict.get("event") == "decision":
        DECISIONS.append(dict(event_dict))
    raise structlog.DropEvent


structlog.configure(processors=[_capture])


async def _one(adapter, prompt: str, tool: str) -> str:
    """Run one prompt through one framework; report what happened to `tool`.

    "The tool did not run" has THREE causes and they must not be conflated:
    governance blocked it, the framework errored before reaching it, or the
    model simply chose not to call it. An early version of this collapsed all
    three into "blocked" -- and duly reported a plain ImportError in the MAF
    adapter as a successful governance block. A demo whose failure mode is a
    false green is worse than no demo.

    GovernanceDenied is therefore caught SPECIFICALLY, and anything else is
    surfaced as an error.
    """
    ran = {"lookup_incident": False, "delete_incident": False}
    DECISIONS.clear()
    try:
        await adapter.run(prompt, ran, tool)
    except GovernanceDenied:
        pass  # some frameworks surface it, some swallow it -- see above
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"

    verdicts = {
        d.get("decision")
        for d in DECISIONS
        if d.get("action") == "tool_call"
        and (d.get("context") or {}).get("tool_name") == tool
    }
    if ran[tool]:
        # Executed. Only honest if governance actually permitted it.
        return "ran" if verdicts <= {"allow"} else "RAN AFTER DENY"
    if "deny" in verdicts or "review" in verdicts:
        return "blocked"
    # Cedar was never asked about this tool: the model did not call it. Not a
    # governance result, and reporting it as one would overstate the demo.
    return "not called"


async def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first -- every framework here calls a real model.")
        raise SystemExit(1)

    rows: list[tuple[str, str, str, str]] = []
    for adapter in ADAPTERS:
        if not adapter.available():
            rows.append((adapter.NAME, adapter.INTEGRATION, "not installed", "not installed"))
            continue
        print(f"running {adapter.NAME} ...", flush=True)
        allowed = await _one(adapter, PROMPT_ALLOW, "lookup_incident")
        denied = await _one(adapter, PROMPT_DENY, "delete_incident")
        rows.append((adapter.NAME, adapter.INTEGRATION, allowed, denied))

    w = max(len(r[1]) for r in rows)
    print(f"\n  prompt A (allowed): {PROMPT_ALLOW}")
    print(f"  prompt B (denied) : {PROMPT_DENY}\n")
    print(f"  {'framework':<14} {'the line you write':<{w}}  {'lookup':<9} delete")
    print(f"  {'-' * 14} {'-' * w}  {'-' * 9} {'-' * 9}")
    for name, integration, a, d in rows:
        print(f"  {name:<14} {integration:<{w}}  {a:<9} {d}")

    live = [r for r in rows if r[2] != "not installed"]
    ok = bool(live) and all(a == "ran" and d == "blocked" for _, _, a, d in live)
    print(
        f"\n  {len(live)} framework(s) ran. "
        + (
            "Same rule, same outcome in every one."
            if ok
            else "MISMATCH -- a framework disagreed. That is the bug this demo exists to catch."
        )
    )
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
