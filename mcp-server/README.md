# parapetai-mcp

MCP server for Claude Code (and other MCP clients) that talks to a Parapet
control plane: device-code login in your browser, then provision governed
agents and pull the exact quickstart config a codebase needs — without
hand-editing env vars.

```
pipx install parapetai-mcp
parapetai-mcp init         # installs parapet-maf/ and parapet-adk/ into .claude/skills/
claude mcp add parapet -e PARAPETAI_CONTROL_PLANE_URL=https://app.parapet.run -- parapetai-mcp serve
```

`pipx`, not `pip` — this is a CLI tool (a `[project.scripts]` entry point), not a
library anything imports, and most Python installs on macOS/Linux now ship as
PEP 668 "externally managed" (Homebrew's Python does), which makes a bare
`pip install parapetai-mcp` fail rather than risk corrupting packages the
system relies on. `pipx` installs CLI tools into their own isolated venv and
puts the entry point on `PATH` — no venv to activate, no `--break-system-packages`
flag needed. No `pipx` available? `python3 -m pip install --user pipx &&
pipx ensurepath` (or `brew install pipx` on macOS), then re-run the command
above. `uv tool install parapetai-mcp` is an equivalent one-liner if `uv` is
already on the machine.

Then in Claude Code: "add Parapet to this project."

## Tools

| Tool | What it does |
|---|---|
| `parapet_login` | Device-code login — open the printed URL, approve in your browser |
| `parapet_whoami` | Who you're authenticated as, and which agents already exist |
| `parapet_provision_agent` | Provision a new governed agent (`agent_id` + one-time `secret`) |
| `parapet_get_quickstart` | The install command / env var names this deployment expects |
| `parapet_list_agents` | Read-only agent listing |
| `parapet_push_policy_file` | Write a Cedar policy file into an agent's bundle (owner/admin role required) |

A `parapet_getting_started` **prompt** is also registered — clients that
list MCP prompts (e.g. Claude Code's `/mcp` menu) can pick it as a
first-run menu: build an example governed-agent app, or do something else
with the tools above. It's discoverable, not auto-fired on connect — no
MCP mechanism forces a prompt onto the first turn.

This server never edits your project's files — it only talks to the
control plane. Three packaged skills tell Claude Code how to use these
tools and where to write the resulting config — `parapet-maf` for a
Microsoft Agent Framework project, `parapet-adk` for a Google ADK one
(the two frameworks put governance in different places, so the
instrumentation steps genuinely differ; Claude Code picks whichever
matches your project), and `parapet-quickdemo` for generating a runnable
governed-vs-ungoverned example from scratch in a new or empty project.

Point at a different control plane (e.g. a local `make dev` instance) with
`PARAPETAI_CONTROL_PLANE_URL` (default `https://app.parapet.run`, the hosted
control plane).
