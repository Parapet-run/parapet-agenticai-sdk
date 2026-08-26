"""Live example: a CLI-shaped MAF agent, several distinct invokers in one
process, each with their OWN identity -- real Cedar policy enforcement
end to end, no mocks in the enforcement path.

Contrast with examples/maf_webapp/: that example is a long-running web
server where IdentityMiddleware wraps ambient identity around each HTTP
request automatically. This one is the other real shape parapetai_agent has
to support -- no web server, no session cookies, nothing HTTP-shaped at
all -- just a process that, in one run, acts on behalf of several
different invokers and needs each one's Cedar decisions kept correctly
separate. That's parapetai_agent.identity_store's job:
set_identity()/use_identity(), not IdentityMiddleware.

Three invokers, real Cedar decisions via the same production
policies/30-identity.cedar role gate examples/maf_webapp/'s Entra scenario
exercises, but with synthetic claims instead of a real Entra sign-in --
`verified_synthetic`, not `verified_live`, matching this repo's own
parapetai-support.yaml distinction (no tenant needed to run this):

  ALLOW -- alice, kind=CUSTOM key "alice": roles=["OrderViewer"] ->
           lookup_order allowed.
  DENY  -- bob, kind=CUSTOM key "bob": roles=[] (identity asserted, but
           insufficient) -> lookup_order denied.
  ALLOW -- carol, kind=THREAD key "desk-1": roles=["OrderViewer"] ->
           lookup_order allowed.
  DENY  -- kind=CUSTOM key "desk-1" (the SAME string carol's THREAD key
           uses, a DIFFERENT identity, roles=[]) -> lookup_order denied.
           Proves IdentityKeyKind actually namespaces keys: two different
           identities stored under the identical string "desk-1" don't
           collide just because kind=THREAD and kind=CUSTOM share a
           backing store.

Run (local dry run against conformance/fake-upstream, no Azure needed --
see README.md "Local dry run"):
    uv run --with fastapi --with uvicorn python3 conformance/fake-upstream/app.py &

    OPENAI_API_KEY=dummy \\
    OPENAI_BASE_URL=http://127.0.0.1:9001/v1 \\
    OPENAI_CHAT_COMPLETION_MODEL=fake-model \\
    uv run --with agent-framework --with "mcp==1.29.0" python3 \\
        examples/maf_cli/run_example.py

Or against real Azure OpenAI -- see README.md "Azure AI Foundry setup".

WIRING is the same minimal shape every other example in this directory
uses now -- see maf_sample_01/'s own module docstring /
docs/maf-integration-pattern.md for the full story -- with ONE
deliberate exception: policy_dir=POLICIES stays explicit (see its own
comment below) instead of omitted, since this example's whole point is
this repo's own real role-gate policy, not the generic bundled default.
agent_id/control_plane_url/agent_secret are all omitted -- set
PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET/PARAPETAI_AGENT_ID in .env to
govern by a real control-plane-provisioned agent's bundle instead -- see
README.md "Its own agent" for why this is a SEPARATE agent from
examples/maf_webapp/'s, not a shared one, verified live end to end
(provision, register, bundle pull, real allow/deny decisions) against a
throwaway control plane while building this.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv

from parapetai_agent import (
    GovernedAgent,
    IdentityKeyKind,
    get_identity,
    set_identity,
    use_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = Path(__file__).resolve().parent
# Unlike maf_sample_01-07 (which omit policy_dir entirely and run on the
# parapetai-agent bundled default), this example's whole point is
# demonstrating alice/bob/carol/front-desk getting DIFFERENT real Cedar
# decisions from THIS repo's own role-gate policy
# (policies/30-identity.cedar) -- so policy_dir=POLICIES stays explicit.
# If PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET are set, GovernedAgent's
# own env-var fallback picks them up automatically and the pulled bundle
# REPLACES POLICIES in memory (see maf.py's build_middleware() docstring)
# -- POLICIES then only serves as the fallback if that first fetch fails.
POLICIES = REPO_ROOT / "policies"
# conformance/mcp-probe/server.py hardcodes port 8765 (FastMCP(port=8765) --
# not configurable via env var or CLI arg), so this example can't run this
# scenario concurrently with examples/maf_webapp/'s own run_example.py,
# same as neither of maf_webapp's own scenarios can run concurrently with
# each other for the same reason.
ORDER_MCP_URL = "http://127.0.0.1:8765/mcp"

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


def _start_mcp_server(
    script: Path, port: int, url: str
) -> tuple[subprocess.Popen[bytes], list[str]]:
    """Same helper as examples/maf_webapp/run_example.py -- returns the
    process plus a live-appended list of its stdout lines, which is how a
    scenario proves a tool call did or didn't reach the MCP server (a
    model's final text is NOT a reliable signal for that)."""
    proc = subprocess.Popen(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [shutil.which("uv"), "run", "--with", "mcp==1.29.0", "python3", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_lines: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log_lines.append(line)

    threading.Thread(target=_drain, daemon=True).start()

    for _ in range(30):
        try:
            httpx.get(url, timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError(f"{script} never came up on :{port} -- check it directly")
    return proc, log_lines


def _call_tool_request_count(log_lines: list[str]) -> int:
    return sum(1 for line in log_lines if "CallToolRequest" in line)


async def _lookup_order_as(
    label: str,
    key: str,
    kind: IdentityKeyKind,
    mcp_tool: MCPStreamableHTTPTool,
    log_lines: list[str],
) -> bool:
    """Runs one governed agent.run() under whatever identity is stored for
    (kind, key) -- set_identity() elsewhere, use_identity() here is the
    entire per-invoker pattern this example exists to demonstrate.

    Every invoker below shares the SAME agent_id (env-resolved / bundled
    default -- see GovernedAgent's own docstring), on purpose -- it's
    this SCRIPT's own software-agent identity (one control-plane-
    provisioned agent, if configured -- see this module's own
    docstring), not a separate registered agent per invoker.
    alice/bob/carol/front-desk are distinguished from each other by
    identity_claims/identity_roles in Cedar's context, exactly the same
    way examples/maf_webapp/'s many concurrent signed-in end users are
    all governed by that ONE app's single agent_id. Returns whether the
    call actually reached the MCP server."""
    lookup_order = next(f for f in mcp_tool.functions if f.name == "lookup_order")
    before = _call_tool_request_count(log_lines)
    async with GovernedAgent(
        client=OpenAIChatCompletionClient(),
        name=f"{label}-agent",
        instructions="You look up orders. Always call the lookup_order tool.",
        tools=[lookup_order],
        policy_dir=POLICIES,
        local_log_dir=EXAMPLE_DIR / "logs",
    ) as agent:
        with use_identity(key, kind=kind):
            result = await agent.run("Look up order 12345.")
    reached = _call_tool_request_count(log_lines) > before
    print(f"{label}: agent said: {result.text!r} -- reached MCP server: {reached}")
    return reached


async def main() -> None:
    order_proc, order_log = _start_mcp_server(
        REPO_ROOT / "conformance" / "mcp-probe" / "server.py", 8765, ORDER_MCP_URL
    )
    try:
        async with MCPStreamableHTTPTool(name="probe", url=ORDER_MCP_URL) as mcp_tool:
            print(
                '\n=== alice: set_identity("alice", roles=["OrderViewer"]) -- '
                "lookup_order should be ALLOWED ==="
            )
            set_identity("alice", claims={"oid": "alice-oid"}, roles=["OrderViewer"])
            if not await _lookup_order_as(
                "alice", "alice", IdentityKeyKind.CUSTOM, mcp_tool, order_log
            ):
                raise RuntimeError("expected alice's lookup_order to reach the MCP server")
            print("PASS: alice (OrderViewer) was allowed.")

            print('\n=== bob: set_identity("bob", roles=[]) -- lookup_order should be DENIED ===')
            set_identity("bob", claims={"oid": "bob-oid"}, roles=[])
            if await _lookup_order_as("bob", "bob", IdentityKeyKind.CUSTOM, mcp_tool, order_log):
                raise RuntimeError("expected bob's lookup_order to be blocked by the role gate")
            print("PASS: bob (identity asserted, no OrderViewer role) was denied.")

            print(
                '\n=== carol: set_identity("desk-1", kind=THREAD, roles=["OrderViewer"]) -- '
                "lookup_order should be ALLOWED ==="
            )
            set_identity(
                "desk-1",
                kind=IdentityKeyKind.THREAD,
                claims={"oid": "carol-oid"},
                roles=["OrderViewer"],
            )
            if not await _lookup_order_as(
                "carol", "desk-1", IdentityKeyKind.THREAD, mcp_tool, order_log
            ):
                raise RuntimeError("expected carol's lookup_order to reach the MCP server")
            print("PASS: carol (OrderViewer, THREAD-keyed) was allowed.")

            print(
                '\n=== collision check: set_identity("desk-1", kind=CUSTOM, roles=[]) -- '
                "the SAME key string as carol's THREAD key, a DIFFERENT identity ==="
            )
            set_identity(
                "desk-1", kind=IdentityKeyKind.CUSTOM, claims={"oid": "front-desk"}, roles=[]
            )
            # THREAD:desk-1 (carol, OrderViewer) must be untouched by the
            # CUSTOM:desk-1 write just above -- this is the actual proof
            # IdentityKeyKind namespaces, not just a docstring claim.
            still_carol = get_identity("desk-1", kind=IdentityKeyKind.THREAD)
            if still_carol is None or "OrderViewer" not in still_carol[1]:
                raise RuntimeError(
                    "THREAD:desk-1 changed after setting CUSTOM:desk-1 -- "
                    "kind namespacing did not isolate the two keys"
                )
            if await _lookup_order_as(
                "front-desk", "desk-1", IdentityKeyKind.CUSTOM, mcp_tool, order_log
            ):
                raise RuntimeError(
                    'expected CUSTOM:"desk-1" to be denied -- it has no OrderViewer role'
                )
            print(
                'PASS: CUSTOM:"desk-1" (no role) was denied while THREAD:"desk-1" (carol, '
                "OrderViewer) stayed allowed -- identical key string, correctly isolated by kind."
            )
    finally:
        order_proc.terminate()
        order_proc.wait(timeout=5)

    print(
        "\nAll invokers processed as expected -- one process, four distinct "
        "identity-store entries, correct per-invoker Cedar decisions."
    )


if __name__ == "__main__":
    asyncio.run(main())
