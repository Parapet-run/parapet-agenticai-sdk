# parapetai-mcp

MCP server for Claude Code (and other MCP clients) that talks to a Parapet
control plane: device-code login in your browser, then provision governed
agents and pull the exact quickstart config a codebase needs — without
hand-editing env vars.

```
pip install parapetai-mcp
parapetai-mcp init         # installs SKILL.md into .claude/skills/parapet/
claude mcp add parapet -- parapetai-mcp serve
```

Then in Claude Code: "add Parapet to this project."

## Tools

| Tool | What it does |
|---|---|
| `parapet_login` | Device-code login — open the printed URL, approve in your browser |
| `parapet_whoami` | Who you're authenticated as, and which agents already exist |
| `parapet_provision_agent` | Provision a new governed agent (`agent_id` + one-time `secret`) |
| `parapet_get_quickstart` | The install command / env var names this deployment expects |
| `parapet_list_agents` | Read-only agent listing |

This server never edits your project's files — it only talks to the
control plane. The packaged `SKILL.md` tells Claude Code how to use these
tools and where to write the resulting config.

Point at a non-default control plane with `PARAPETAI_CONTROL_PLANE_URL`
(default `http://localhost:8090`).
