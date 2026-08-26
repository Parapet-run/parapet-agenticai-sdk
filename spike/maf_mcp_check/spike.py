"""MAF FunctionMiddleware spike -- one script, six labeled runs, real output.

Answers, empirically, against real Microsoft Agent Framework code (package
`agent_framework`) and a real MCP server (conformance/mcp-probe/server.py,
reused unmodified -- see stdio_server.py for why a separate stdio entrypoint
was needed instead of editing that file):

  1. Do MCP tools hit the same FunctionMiddleware as native tools?
  2. Does FunctionInvocationContext expose the real tool name and arguments
     pre-execution?
  3. Does not calling call_next() actually prevent execution?
  4. Can the source (native vs MCP, and which MCP server) be distinguished
     from the context?
  5. Does this hold for stdio MCP as well as Streamable HTTP?

LLM backend: conformance/fake-upstream (deterministic canned responses, no
credentials needed) -- this spike is about MAF's own middleware, independent
of the AGT gateway, so the fake upstream is used directly, not through the
gateway.

Prerequisites (started separately, see docs/maf-middleware-spike.md):
  - conformance/fake-upstream/app.py running on :9001
  - conformance/mcp-probe/server.py running on :8765 (streamable-http)
  - OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_CHAT_COMPLETION_MODEL set

Run: uv run --with agent-framework --with "mcp==1.29.0" python3 spike.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_framework import Agent, FunctionMiddleware, MCPStdioTool, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatCompletionClient
from middleware import LoggingFunctionMiddleware
from native_tool import get_server_status, reset, was_actually_called

MCP_HTTP_URL = "http://127.0.0.1:8765/mcp"
STDIO_SERVER = str(Path(__file__).resolve().parent / "stdio_server.py")
UV = shutil.which("uv")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


class SourceIdentifyingMiddleware(FunctionMiddleware):
    """Same logging behavior as LoggingFunctionMiddleware, plus the closure
    introspection that identifies *which* MCP server (or that it's native)
    a call came from -- undocumented (agent_framework doesn't expose this as
    a stable public attribute), but real and reproducible: MCPTool wraps
    each remote tool in a closure over `self`, so `function.func.__closure__`
    holds a direct reference to the originating MCPStreamableHTTPTool /
    MCPStdioTool instance for MCP-sourced tools, and is absent/different for
    native ones."""

    async def process(self, context, call_next):  # type: ignore[no-untyped-def]
        fn = context.function
        source = "native (no MCP closure)"
        closure = getattr(fn.func, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    val = cell.cell_contents
                except ValueError:
                    continue
                if type(val).__name__ in ("MCPStreamableHTTPTool", "MCPStdioTool"):
                    source = f"{type(val).__name__}(name={val.name!r})"
                    break
        print(f"[identify] name={fn.name!r} additional_properties={fn.additional_properties!r}")
        print(f"[identify] source={source}")
        await call_next()


async def run_a_native_unblocked() -> None:
    section("RUN A -- native tool, unblocked (Q1, Q2, Q4-native-baseline)")
    reset()
    async with Agent(
        client=OpenAIChatCompletionClient(),
        name="SpikeAgent",
        instructions="Use the tool.",
        tools=[get_server_status],
        middleware=[LoggingFunctionMiddleware()],
    ) as agent:
        result = await agent.run("What is the server status?")
        print(f"RESULT: {result.text}")
        print(f"native function actually executed: {was_actually_called()}")


async def run_b_native_blocked() -> None:
    section("RUN B -- native tool, BLOCKED (Q3)")
    reset()
    async with Agent(
        client=OpenAIChatCompletionClient(),
        name="SpikeAgent",
        instructions="Use the tool.",
        tools=[get_server_status],
        middleware=[LoggingFunctionMiddleware(blocked={"get_server_status"})],
    ) as agent:
        result = await agent.run("What is the server status?")
        print(f"RESULT: {result.text}")
        print(f"native function actually executed (should be False): {was_actually_called()}")


async def run_c_mcp_http_unblocked() -> None:
    section("RUN C -- MCP Streamable HTTP tool, unblocked (Q1, Q2, Q4)")
    async with (
        MCPStreamableHTTPTool(name="probe-http", url=MCP_HTTP_URL) as mcp_tool,
        Agent(
            client=OpenAIChatCompletionClient(),
            name="SpikeAgent",
            instructions="Use the tool.",
            tools=mcp_tool,
            middleware=[SourceIdentifyingMiddleware()],
        ) as agent,
    ):
        result = await agent.run("Look up order 12345.")
        print(f"RESULT: {result.text}")


async def run_d_mcp_http_blocked() -> None:
    section("RUN D -- MCP Streamable HTTP tool, BLOCKED (Q3 for MCP)")
    async with (
        MCPStreamableHTTPTool(name="probe-http", url=MCP_HTTP_URL) as mcp_tool,
        Agent(
            client=OpenAIChatCompletionClient(),
            name="SpikeAgent",
            instructions="Use the tool.",
            tools=mcp_tool,
            middleware=[LoggingFunctionMiddleware(blocked={"lookup_order"})],
        ) as agent,
    ):
        result = await agent.run("Look up order 12345.")
        print(f"RESULT: {result.text}")
        print("(verify manually: MCP server log shows no CallToolRequest for this run)")


async def run_e_mcp_stdio_unblocked() -> None:
    section("RUN E -- MCP stdio tool, same server.py tool defs (Q5)")
    async with (
        MCPStdioTool(
            name="probe-stdio",
            command=UV,
            args=["run", "--with", "mcp==1.29.0", "python3", STDIO_SERVER],
        ) as mcp_tool,
        Agent(
            client=OpenAIChatCompletionClient(),
            name="SpikeAgent",
            instructions="Use the tool.",
            tools=mcp_tool,
            middleware=[SourceIdentifyingMiddleware()],
        ) as agent,
    ):
        result = await agent.run("Look up order 12345.")
        print(f"RESULT: {result.text}")


async def run_f_duplicate_names() -> None:
    section("RUN F -- two MCP sources, colliding tool names (Q4 edge case)")
    try:
        async with (
            MCPStreamableHTTPTool(name="server-http", url=MCP_HTTP_URL) as http_tool,
            MCPStdioTool(
                name="server-stdio",
                command=UV,
                args=["run", "--with", "mcp==1.29.0", "python3", STDIO_SERVER],
            ) as stdio_tool,
            Agent(
                client=OpenAIChatCompletionClient(),
                name="SpikeAgent",
                instructions="Use the tool.",
                tools=[http_tool, stdio_tool],
                middleware=[SourceIdentifyingMiddleware()],
            ) as agent,
        ):
            await agent.run("Look up order 12345.")
    except ValueError as e:
        print(f"EXPECTED failure (both servers expose 'lookup_order'): {e}")


async def main() -> None:
    await run_a_native_unblocked()
    await run_b_native_blocked()
    await run_c_mcp_http_unblocked()
    await run_d_mcp_http_blocked()
    await run_e_mcp_stdio_unblocked()
    await run_f_duplicate_names()


if __name__ == "__main__":
    asyncio.run(main())
