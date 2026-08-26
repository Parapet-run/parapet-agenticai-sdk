"""Console-script entry point (`parapetai-mcp`). Two subcommands:

  serve  -- run the MCP server over stdio (what an MCP client, e.g.
            Claude Code via `claude mcp add`, actually launches).
  init   -- one-time setup in a target project: copies the packaged
            SKILL.md into .claude/skills/parapet/ and prints the
            `claude mcp add` line to wire this server in. Does NOT touch
            any file outside .claude/skills/ -- instrumenting the
            project's own code is the calling agent's job, guided by the
            skill it just installed, not this command's.
"""

from __future__ import annotations

import argparse
import importlib.resources
import shutil
import sys
from pathlib import Path


def _cmd_serve(_args: argparse.Namespace) -> None:
    from parapetai_mcp.server import main as run_server

    run_server()


def _cmd_init(args: argparse.Namespace) -> None:
    target_dir = Path(args.project_dir).resolve() / ".claude" / "skills" / "parapet"
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_src = importlib.resources.files("parapetai_mcp.skills") / "SKILL.md"
    shutil.copyfile(str(skill_src), target_dir / "SKILL.md")
    print(f"Installed SKILL.md to {target_dir / 'SKILL.md'}")
    print()
    print("Add this MCP server to Claude Code:")
    print("  claude mcp add parapet -- parapetai-mcp serve")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="parapetai-mcp")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    init = sub.add_parser("init", help="install SKILL.md into a project's .claude/skills/")
    init.add_argument("project_dir", nargs="?", default=".")
    init.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
