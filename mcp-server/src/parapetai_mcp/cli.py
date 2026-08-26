"""Console-script entry point (`parapetai-mcp`). Two subcommands:

serve  -- run the MCP server over stdio (what an MCP client, e.g.
          Claude Code via `claude mcp add`, actually launches).
init   -- one-time setup in a target project: copies BOTH packaged
          skills -- parapet-maf/ and parapet-adk/ -- into
          .claude/skills/, and prints the `claude mcp add` line to wire
          this server in. Does NOT touch any file outside
          .claude/skills/ -- instrumenting the project's own code is
          the calling agent's job, guided by whichever skill actually
          matches the project (agent_framework vs google.adk), not this
          command's. Both are installed unconditionally rather than
          this command trying to detect which framework the target
          project uses -- that detection belongs to the calling agent,
          which can actually read the project's code; this command
          can't reliably guess it from a bare directory path alone, and
          a wrong guess would silently install only the wrong skill.
"""

from __future__ import annotations

import argparse
import importlib.resources
import shutil
import sys
from pathlib import Path

_SKILLS = ("maf", "adk")


def _cmd_serve(_args: argparse.Namespace) -> None:
    from parapetai_mcp.server import main as run_server

    run_server()


def _cmd_init(args: argparse.Namespace) -> None:
    project_root = Path(args.project_dir).resolve()
    installed: list[Path] = []
    for skill_name in _SKILLS:
        target_dir = project_root / ".claude" / "skills" / f"parapet-{skill_name}"
        target_dir.mkdir(parents=True, exist_ok=True)
        skill_src = importlib.resources.files("parapetai_mcp.skills") / skill_name / "SKILL.md"
        dest = target_dir / "SKILL.md"
        shutil.copyfile(str(skill_src), dest)
        installed.append(dest)

    for dest in installed:
        print(f"Installed SKILL.md to {dest}")
    print()
    print(
        "Two skills installed -- parapet-maf (Microsoft Agent Framework) and"
        " parapet-adk (Google ADK). Each one only applies to a project using"
        " that framework; an agent picks the right one by inspecting the"
        " target project, not from this command."
    )
    print()
    print("Add this MCP server to Claude Code:")
    print(
        "  claude mcp add parapet"
        " -e PARAPETAI_CONTROL_PLANE_URL=https://app.parapet.run"
        " -- parapetai-mcp serve"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="parapetai-mcp")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.set_defaults(func=_cmd_serve)

    init = sub.add_parser(
        "init", help="install both parapet-maf/parapet-adk SKILL.md files into .claude/skills/"
    )
    init.add_argument("project_dir", nargs="?", default=".")
    init.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
