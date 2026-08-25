"""A genuine Streamable HTTP MCP server -- not a mock. Investigation fixture
for docs/mcp-interception.md, proving the gateway's existing MCPParser +
Cedar tool_call rules against a real MCP server, not a stand-in.

FastMCP's default streamable_http_path is "/mcp", matching MCPParser.matches()
(path.startswith("/mcp")) with zero path rewriting needed.

execute_shell is named to match policies/20-tools.cedar's forbidden list
exactly (tool_destructive_denied). It never runs anything -- a test fixture,
not a real capability -- the whole point is proving Cedar denies the call
before it would ever reach this function.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="mcp-interception-probe", host="0.0.0.0", port=8765)  # noqa: S104


@mcp.tool()
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


@mcp.tool()
def execute_shell(command: str) -> str:
    """Deliberately named to match the Cedar forbidden list. Never actually
    runs anything -- proves the gateway denies this before it would arrive
    here, not that this function is safe to call."""
    return "not actually run"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
