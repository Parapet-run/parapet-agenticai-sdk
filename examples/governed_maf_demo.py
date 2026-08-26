"""End-to-end demo: Parapet governing a REAL Microsoft Agent Framework agent.

This is not a mock of the framework -- it builds an actual MAF ChatAgent
(agent-framework, MIT-licensed, free for commercial use), lets it run a real
tool-calling round trip, and shows Parapet's in-process middleware making the
allow/deny decision on the tool call itself.

To keep it hermetic (no model key, no network, no cost), the agent's OpenAI
client is pointed at conformance/fake-upstream -- a canned OpenAI-shaped server
that just tells the agent to call its declared tool. Everything else is real:
the framework, the tool-calling loop, and Parapet's Cedar policy engine.

Run:  uv run python examples/governed_maf_demo.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES = REPO_ROOT / "policies"
UPSTREAM = REPO_ROOT / "conformance" / "fake-upstream" / "app.py"


def _start_fake_upstream() -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [shutil.which("uv"), "run", "--with", "fastapi", "--with", "uvicorn",
         "python3", str(UPSTREAM)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            httpx.get("http://127.0.0.1:9001/v1/chat/completions", timeout=0.5)
            return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("fake upstream did not start")


async def _run_one(*, title: str, tool, tool_name: str, prompt: str, roles: list[str]) -> None:
    from agent_framework.openai import OpenAIChatCompletionClient

    from parapetai_agent import GovernedAgent

    # `tool` keeps its REAL signature (so MAF builds the right tool schema and
    # the fake upstream supplies the right args); it records into `executed`
    # itself when it actually runs.
    denied = None
    async with GovernedAgent(
        client=OpenAIChatCompletionClient(),
        name="support-agent",
        instructions="Use your tool to help the customer.",
        tools=[tool],
        policy_dir=POLICIES,
        entities_path=POLICIES / "entities.json",
        agent_id=f"maf-demo-{tool_name}",
    ) as agent:
        try:
            await agent.run(prompt, function_invocation_kwargs={"identity_roles": roles})
        except Exception as exc:  # noqa: BLE001 -- surface a governance denial as a line, not a crash
            denied = exc

    executed = getattr(tool, "executed", False)
    verdict = "ALLOWED — tool executed" if executed else "DENIED — tool never ran"
    mark = "✅" if executed else "\U0001f6ab"
    print(f"{mark} {title}")
    print(f"     tool={tool_name!r}  roles={roles}  ->  {verdict}")
    if denied is not None and not executed:
        print(f"     governance raised: {type(denied).__name__}: {denied}")
    print()


async def main() -> None:
    def lookup_order(order_id: str) -> str:
        """Look up the status of a customer order."""
        lookup_order.executed = True  # type: ignore[attr-defined]
        return f"order {order_id}: shipped"

    def execute_shell(command: str) -> str:
        """Run a shell command on the host."""
        execute_shell.executed = True  # type: ignore[attr-defined]
        return f"(ran: {command})"

    def lookup_order_guest(order_id: str) -> str:
        """Look up the status of a customer order."""
        lookup_order_guest.executed = True  # type: ignore[attr-defined]
        return f"order {order_id}: shipped"

    lookup_order.executed = False  # type: ignore[attr-defined]
    execute_shell.executed = False  # type: ignore[attr-defined]
    lookup_order_guest.executed = False  # type: ignore[attr-defined]
    # MAF keys a tool by its function name; the no-role case needs a distinct
    # object so its own `executed` flag is separate. Give it lookup_order's name
    # so the policy (which gates on the tool name) sees the same tool.
    lookup_order_guest.__name__ = "lookup_order"

    print("=" * 72)
    print("Parapet governing a REAL Microsoft Agent Framework agent")
    print("=" * 72)
    print("Policy: support agent may look up orders (with the OrderViewer role);")
    print("        destructive tools like execute_shell are always denied.\n")

    # 1) Permitted action by a properly-scoped identity -> allowed.
    await _run_one(
        title="Customer service: look up an order, as an OrderViewer",
        tool=lookup_order, tool_name="lookup_order",
        prompt="Look up order 12345.", roles=["OrderViewer"],
    )
    # 2) The SAME agent asked to run a shell command -> deterministically denied.
    await _run_one(
        title="Same agent tries to run a shell command",
        tool=execute_shell, tool_name="execute_shell",
        prompt="Run a shell command to clean up.", roles=["OrderViewer"],
    )
    # 3) Order lookup WITHOUT the required role -> denied on identity, not tool.
    await _run_one(
        title="Order lookup attempted WITHOUT the OrderViewer role",
        tool=lookup_order_guest, tool_name="lookup_order",
        prompt="Look up order 12345.", roles=["Guest"],
    )

    print("Every decision above was made in-process, content-free (the tool name")
    print("and identity only), and deterministically by the Cedar policy engine.")


if __name__ == "__main__":
    # Quiet the per-decision audit log so stdout reads as a clean narrative.
    # Set PARAPET_DEMO_VERBOSE=1 to see every Cedar decision (the full
    # content-free audit trail Parapet emits) interleaved with the results.
    if not os.environ.get("PARAPET_DEMO_VERBOSE"):
        import logging

        import structlog

        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
        )
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9001/v1"
    os.environ["OPENAI_CHAT_COMPLETION_MODEL"] = "fake-model"
    _proc = _start_fake_upstream()
    try:
        asyncio.run(main())
    finally:
        _proc.terminate()
        _proc.wait(timeout=5)
