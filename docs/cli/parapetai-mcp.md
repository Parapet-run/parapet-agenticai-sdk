# parapetai-mcp

`parapetai-mcp` is an [MCP](https://modelcontextprotocol.io/) server for
Claude Code (and any other MCP client): device-code login to a Parapet
control plane, provisioning agents, pulling the exact quickstart config a
codebase needs, and six packaged **skills** that scaffold, retrofit, or
audit a governed agent project for you.

It ships as its own PyPI package, separate from `parapetai-agent` — you
don't need it to use the SDK, and using it never requires writing
Cedar-aware code by hand if you'd rather have an agent do it.

## Install

It's a CLI tool (a `[project.scripts]` entry point), not a library
anything imports — and most Python installs on macOS/Linux now ship as
[PEP 668](https://peps.python.org/pep-0668/) "externally managed", which
makes a bare `pip install parapetai-mcp` fail. Use
[`pipx`](https://pipx.pypa.io/):

```bash
pipx install parapetai-mcp
```

No `pipx`? `brew install pipx` on macOS, or
`python3 -m pip install --user pipx && pipx ensurepath` elsewhere. If
[`uv`](https://docs.astral.sh/uv/) is already on the machine,
`uv tool install parapetai-mcp` is an equivalent one-liner.

Requires Python **3.12+**.

## Set up a project

```bash
parapetai-mcp init                 # installs all packaged parapet-* skills into .claude/skills/
claude mcp add parapet -e PARAPETAI_CONTROL_PLANE_URL=https://app.parapet.run -- parapetai-mcp serve
```

- `init [project_dir]` (default `.`) copies every packaged skill —
  `parapet-maf`, `parapet-adk`, `parapet-quickdemo`, `parapet-install-prereqs`,
  `parapet-audit`, `parapet-audit-fix`
  — into `<project_dir>/.claude/skills/parapet-<name>/`. It never touches
  anything outside `.claude/skills/`; it does not instrument your project's
  code itself. That's what the skills themselves do once invoked.
- `claude mcp add ...` registers `parapetai-mcp serve` as an MCP server
  with Claude Code, pointed at a control plane (defaults to
  `https://app.parapet.run` if you omit the `-e` override).
- `serve` (no other flags) runs the MCP server over stdio — this is what
  an MCP client actually launches; you don't run it by hand.

Once connected, ask Claude Code to build a demo, add Parapet to an
existing MAF/ADK project, or audit an existing codebase for ungoverned
model/tool calls, and the relevant skill takes it from there — see
[Skills](skills.md) for what each one does, and [MCP tools](mcp-tools.md)
for the individual tools they call along the way.

## Environment

| Variable | Default | Controls |
|---|---|---|
| `PARAPETAI_CONTROL_PLANE_URL` | `https://app.parapet.run` | The control plane every `parapet_*` tool talks to, unless a per-call argument overrides it |
| `PARAPETAI_MCP_CONFIG_DIR` | `~/.parapet` | Where the CLI token (`credentials.json`, one entry per control-plane URL) is stored after `parapet_login` |

Never paste a token, agent secret, or any other credential into chat — the
login flow is designed specifically to avoid that: `parapet_login` opens a
browser approval page and polls until you approve, and never returns the
credential itself to the calling model.

## Source

MIT licensed, part of the same repo as the SDK:
[`github.com/Parapet-run/parapet-agenticai-sdk/tree/main/mcp-server`](https://github.com/Parapet-run/parapet-agenticai-sdk/tree/main/mcp-server).
