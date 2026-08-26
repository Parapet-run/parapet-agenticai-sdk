"""Websocket-transport wrapper around conformance/mcp-probe/server.py's FastMCP
instance -- reused, not copied, same pattern as stdio_server.py (that file's
docstring explains why server.py itself is never edited to add a transport).

mcp.server.websocket is DEPRECATED as of mcp==1.29.0 (the version this repo
pins everywhere): "WebSocket was never part of the MCP specification; use the
streamable HTTP transport instead," with removal planned for mcp 2.0. This
file exists to prove ParapetFunctionMiddleware enforces identically on this
transport too -- for anyone currently stuck on it -- not as an endorsement to
build new integrations against it. See parapetai-agent/tests/test_maf.py's
TestToolSourcesLiveEndToEnd (test_mcp_websocket_tool_*) for what this backs.

FastMCP.run() itself has no "websocket" transport option (only stdio/sse/
streamable-http), so unlike stdio_server.py's one-line mcp.run(transport=...),
this has to drive the lower-level pieces directly: mcp.server.websocket's
ASGI websocket_server() context manager wraps the raw ASGI scope/receive/send
into the same (read_stream, write_stream) pair FastMCP's own transports
produce, then FastMCP._mcp_server.run() (the actual MCP session loop, shared
by every transport) takes it from there -- reusing server.py's registered
tools unchanged.

A bare ASGI3 callable, not a Starlette app -- confirmed empirically that
Starlette's WebSocketRoute always wraps a plain function as func(websocket)
(one argument, a starlette.websockets.WebSocket), with no passthrough for a
raw (scope, receive, send) handler the way Route has for HTTP; websocket_server
needs the raw ASGI triple directly. Skipping Starlette's routing avoids
fighting that mismatch for what is otherwise a one-route server.
"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.websocket import websocket_server
from starlette.types import Receive, Scope, Send

_MCP_PROBE_DIR = Path(__file__).resolve().parents[2] / "conformance" / "mcp-probe"
sys.path.insert(0, str(_MCP_PROBE_DIR))

from server import mcp  # noqa: E402

# DNS-rebinding protection (default: on, with an empty allowed_origins list)
# rejects agent_framework's websocket client with a 403 -- it sends an
# Origin header this middleware's default allowlist doesn't cover. This is
# a local, single-purpose test fixture (same trust model as
# conformance/mcp-probe/server.py, which ships with no auth either), not a
# server anything untrusted ever reaches, so the protection is turned off
# here rather than reverse-engineered into an allowlist that would just be
# guessing at what a real client sends.
_NO_DNS_REBINDING_PROTECTION = TransportSecuritySettings(enable_dns_rebinding_protection=False)


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "http":
        # Plain HTTP has no MCP meaning here -- exists so a readiness probe
        # (a GET, which can't complete a websocket handshake) gets a real,
        # fast response instead of the connection just hanging/resetting.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        return
    if scope["type"] != "websocket" or scope["path"] != "/mcp":
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})
        return
    async with websocket_server(
        scope, receive, send, security_settings=_NO_DNS_REBINDING_PROTECTION
    ) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
