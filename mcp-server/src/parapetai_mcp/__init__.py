"""parapetai-mcp: MCP server for Claude Code (and other MCP clients) that
authenticates a developer against a Parapet control plane (device-code
login, approved in the browser) and provisions/inspects agents on their
behalf. Ships three packaged SKILL.md files (see cli.py's `init` command),
one per in-process framework integration parapetai-agent supports --
parapet-maf (Microsoft Agent Framework), parapet-adk (Google ADK), and
parapet-langgraph (LangGraph/LangChain) -- that teach an MCP client when
and how to use these tools. Three files, not one, because the frameworks
put governance in genuinely different places (MAF and LangGraph: every
Agent(...)/create_agent(...) construction individually; ADK: the Runner
once, covering every agent underneath it) -- conflating the instructions
would tell an agent instrumenting one framework to verify the wrong
thing. The tools themselves never edit a target project's files; that
stays the calling agent's own job, guided by whichever skill matches the
project.
"""

from __future__ import annotations
