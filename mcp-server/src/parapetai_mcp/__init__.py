"""parapetai-mcp: MCP server for Claude Code (and other MCP clients) that
authenticates a developer against a Parapet control plane (device-code
login, approved in the browser) and provisions/inspects agents on their
behalf. Ships a packaged SKILL.md (see cli.py's `init` command) that
teaches an MCP client when and how to use these tools -- the tools
themselves never edit a target project's files; that stays the calling
agent's own job, guided by the skill.
"""

from __future__ import annotations
