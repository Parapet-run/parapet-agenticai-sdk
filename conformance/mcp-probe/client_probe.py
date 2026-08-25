"""Investigation probe for docs/mcp-interception.md.

Phase 1: connect directly to server.py, bypassing the gateway entirely --
proves the fixture is a real, working Streamable HTTP MCP server before
testing anything about the gateway against it.

Phase 2: connect through the gateway's /a/{agent_id}/mcp path and call the
benign tool, then the destructive one. Prints raw responses/exceptions so
both outcomes are visible directly, not asserted -- this is an investigation
artifact, not a test suite.

Known, documented gap this exercises rather than works around: the gateway
has no MCP upstream configured (see docs/mcp-interception.md), so even an
*allowed* call 502s after parsing -- that 502 is expected here, not a probe
bug. The destructive call is expected to 403 without ever reaching that code
path at all.

Usage:
    python conformance/mcp-probe/server.py        # in one shell
    make dev                                       # in another (PARAPETAI_MODE=enforce)
    python conformance/mcp-probe/client_probe.py [gateway_url]
"""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = "http://127.0.0.1:8765/mcp"
GATEWAY = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


async def phase1_direct() -> None:
    print("--- phase 1: direct connection to the real MCP server (no gateway) ---")
    async with streamablehttp_client(SERVER_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"tools: {[t.name for t in tools.tools]}")
            result = await session.call_tool("lookup_order", {"order_id": "12345"})
            print(f"lookup_order result: {result.content}")
    print("PASS: server.py is a real, working Streamable HTTP MCP server\n")


def phase2_through_gateway() -> None:
    print("--- phase 2: through the gateway, raw JSON-RPC (no MCP client needed --")
    print("    the gateway parses/evaluates before any MCP handshake occurs) ---")
    agent_id = f"mcp-probe-{uuid4()}"
    url = f"{GATEWAY}/a/{agent_id}/mcp"

    print(f"agent_id: {agent_id}\n")

    print("-> benign call: lookup_order")
    resp = httpx.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "lookup_order", "arguments": {"order_id": "12345"}},
        },
        timeout=10,
    )
    print(f"   status={resp.status_code} body={resp.text[:300]}")
    print("   expected: parsed OK, then 502 no_upstream (documented gap, not a bug)\n")

    print("-> destructive call: execute_shell")
    resp2 = httpx.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "execute_shell", "arguments": {"command": "echo hi"}},
        },
        timeout=10,
    )
    print(f"   status={resp2.status_code} body={resp2.text[:300]}")
    print("   expected: 403, denied by policies/20-tools.cedar before upstream lookup\n")

    print(f"verify with: curl '{GATEWAY}/__parapetai/observations?agent_id={agent_id}'")


async def main() -> None:
    await phase1_direct()
    phase2_through_gateway()


if __name__ == "__main__":
    asyncio.run(main())
