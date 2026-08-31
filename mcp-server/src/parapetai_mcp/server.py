"""The MCP tool surface. Every tool is a thin wrapper over client.py ->
the control plane's CLI API -- no provisioning/auth logic lives here.

parapet_login is the one tool that ever sees a raw cli_token: it comes
back from ControlPlaneClient.poll_device_code() as a Python value inside
this process and is written straight to disk via config.set_cli_token()
in the same function call. It is deliberately never included in this
tool's *return value* -- only a human-readable status string is -- so it
never round-trips back through the calling model's context.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from typing import Any

from mcp.server.fastmcp import FastMCP

from parapetai_mcp.audit import audit_codebase
from parapetai_mcp.client import ControlPlaneClient, NotLoggedInError
from parapetai_mcp.config import DEFAULT_CONTROL_PLANE_URL, set_cli_token

mcp = FastMCP("parapetai")

_POLL_INTERVAL_SECONDS = 2


@mcp.tool()
async def parapet_login(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> str:
    """Authenticate as yourself against a Parapet control plane. Opens the
    approval page in your default browser (falls back to just printing the
    URL if that fails -- e.g. no GUI available) -- sign in if needed, and
    approve. This tool then polls until you do, and stores the resulting
    credential locally; it never returns the credential itself."""
    client = ControlPlaneClient(control_plane_url)
    started = await client.start_device_code()
    verification_uri = started["verification_uri"]
    user_code = started["user_code"]
    device_code = started["device_code"]
    expires_in = started["expires_in"]

    # webbrowser.open() can raise (or just return False) on a headless box,
    # inside a sandboxed subprocess with no display, etc. -- never let that
    # take down the login flow itself; the URL is always printed either way.
    try:
        opened = webbrowser.open(verification_uri)
    except Exception:  # noqa: BLE001 -- best-effort, see above
        opened = False

    if opened:
        prompt = (
            f"Opened {verification_uri} in your browser (code: {user_code}) -- "
            f"approve the login there. Waiting up to {expires_in}s..."
        )
    else:
        prompt = (
            f"Couldn't open a browser automatically -- open {verification_uri} "
            f"(code: {user_code}) yourself and approve the login. "
            f"Waiting up to {expires_in}s..."
        )

    elapsed = 0
    while elapsed < expires_in:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        result = await client.poll_device_code(device_code)
        status = result["status"]
        if status == "approved":
            set_cli_token(control_plane_url, result["cli_token"], account_id=result["account_id"])
            return f"Logged in to {control_plane_url} (account {result['account_id']})."
        if status in ("denied", "gone", "expired"):
            return f"{prompt}\nLogin {status} -- run parapet_login again to retry."

    return f"{prompt}\nTimed out waiting for approval -- run parapet_login again to retry."


@mcp.tool()
async def parapet_whoami(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> dict[str, Any]:
    """Who you're authenticated as, and which agents already exist in
    your account. Run parapet_login first if this reports not logged in."""
    client = ControlPlaneClient(control_plane_url)
    try:
        return await client.whoami()
    except NotLoggedInError as exc:
        return {"error": str(exc)}


@mcp.tool()
async def parapet_get_quickstart(
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL,
) -> dict[str, str]:
    """The exact SDK install command, minimum Python version, default
    model, and env var names this control plane deployment expects --
    always read from the deployment's own config, never hardcoded, so it
    can't drift from what `pip install` actually resolves to here."""
    client = ControlPlaneClient(control_plane_url)
    return await client.get_quickstart()


@mcp.tool()
async def parapet_provision_agent(
    display_name: str | None = None,
    tenant_id: str | None = None,
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL,
) -> dict[str, Any]:
    """Provision a new governed agent in your account. Returns
    {agent_id, secret} -- the secret is shown exactly once here, the same
    as everywhere else in this system; write it straight into the target
    project's env config, don't just print it and move on."""
    client = ControlPlaneClient(control_plane_url)
    try:
        return await client.provision_agent(display_name=display_name, tenant_id=tenant_id)
    except NotLoggedInError as exc:
        return {"error": str(exc)}
    except PermissionError as exc:
        return {"error": str(exc)}


@mcp.tool()
async def parapet_list_agents(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> dict[str, Any]:
    """Read-only: the agents already provisioned in your account."""
    client = ControlPlaneClient(control_plane_url)
    try:
        who = await client.whoami()
    except NotLoggedInError as exc:
        return {"error": str(exc)}
    return {"agents": who["agents"]}


@mcp.tool()
async def parapet_push_policy_file(
    agent_id: str,
    filename: str,
    content: str,
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL,
) -> dict[str, Any]:
    """Write one Cedar policy file into an already-provisioned agent's
    bundle (creates it if the filename is new, overwrites if it already
    exists). Requires an owner/admin role -- a viewer's token gets a
    PermissionError surfaced as {"error": ...}, same shape as every other
    tool here. Use this instead of asking the human to paste policy into
    the console by hand when you already generated the .cedar content
    yourself (e.g. the parapet-quickdemo skill)."""
    client = ControlPlaneClient(control_plane_url)
    try:
        return await client.push_bundle_file(agent_id, filename, content)
    except NotLoggedInError as exc:
        return {"error": str(exc)}
    except PermissionError as exc:
        return {"error": str(exc)}


@mcp.tool()
def parapet_audit_codebase(path: str, output_dir: str | None = None) -> dict[str, Any]:
    """Local, static AST scan of a Python codebase -- no control plane
    call, nothing sent anywhere, same locality guarantee as
    parapet_check_prerequisites -- for ungoverned model/tool-call sites:
    a raw agent_framework.Agent or google.adk.runners.Runner/
    InMemoryRunner construction (especially one with tools=), a
    build_middleware()/build_plugin() result never registered, a raw
    openai/anthropic/google.genai client with no governance visible in
    the same file, or a GovernedAgent/GovernedRunner relying only on the
    SDK's generic bundled default policy. Each finding is scored high/
    medium/low and written to a Markdown report at
    {output_dir or path/.parapet/audit}/report.md (also returned inline
    in `findings`). Deliberately favors precision over recall -- see the
    report's own header before treating a clean result as a certification.
    Run the parapet-audit-fix skill afterward to apply fixes and confirm
    the finding count actually goes down."""
    return audit_codebase(path, output_dir)


def _python_check() -> dict[str, Any]:
    # Checks real binaries on PATH, not this process's own interpreter --
    # parapetai-mcp normally runs inside a pipx-managed venv, which says
    # nothing about what a NEW venv (e.g. a generated quickdemo project)
    # would resolve `python3`/`python3.12` to system-wide.
    for candidate in ("python3.13", "python3.12", "python3"):
        path = shutil.which(candidate)
        if not path:
            continue
        try:
            out = subprocess.run(  # noqa: S603 -- fixed candidate list, not untrusted input
                [path, "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        match = re.search(r"(\d+)\.(\d+)", out)
        if not match:
            continue
        major, minor = int(match.group(1)), int(match.group(2))
        ok = (major, minor) >= (3, 12)
        return {"ok": ok, "detail": f"{out} ({candidate} at {path})"}
    return {"ok": False, "detail": "no python3.12+ found on PATH"}


def _which_check(binary: str) -> dict[str, Any]:
    path = shutil.which(binary)
    return {"ok": path is not None, "detail": path or f"{binary} not found on PATH"}


@mcp.tool()
def parapet_check_prerequisites() -> dict[str, Any]:
    """Local machine check -- no control plane call, nothing sent anywhere --
    for what every parapet-* skill assumes is already installed: Python
    3.12+, pipx, and uv. Detects the real OS and (on Linux) which package
    manager is actually present, so the returned install_cmd is never a
    guess -- e.g. `apt` on a Fedora box would be wrong. Does NOT install
    anything itself; report the results and ask before running any install
    command yourself (see the parapet-install-prereqs skill)."""
    system = platform.system()  # "Darwin" | "Linux" | "Windows"
    checks: dict[str, dict[str, Any]] = {"python": _python_check()}

    if system == "Darwin":
        os_name = "macos"
        checks["homebrew"] = _which_check("brew")
        if not checks["homebrew"]["ok"]:
            checks["homebrew"]["install_cmd"] = (
                '/bin/bash -c "$(curl -fsSL '
                'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
            )
        pipx_cmd = "brew install pipx && pipx ensurepath"
        uv_cmd = "brew install uv"
        python_cmd = "brew install python@3.12"
    elif system == "Linux":
        os_name = "linux"
        if shutil.which("apt") or shutil.which("apt-get"):
            pkg_mgr, python_cmd = (
                "apt",
                "sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip",
            )
        elif shutil.which("dnf"):
            pkg_mgr, python_cmd = "dnf", "sudo dnf install -y python3.12"
        else:
            pkg_mgr, python_cmd = None, "install Python 3.12+ via your distro's package manager"
        checks["package_manager"] = {
            "ok": pkg_mgr is not None,
            "detail": pkg_mgr or "no apt/dnf found -- can't suggest an exact command",
        }
        pipx_cmd = "python3 -m pip install --user pipx && python3 -m pipx ensurepath"
        uv_cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    elif system == "Windows":
        os_name = "windows"
        checks["winget"] = _which_check("winget")
        pipx_cmd = "py -m pip install --user pipx; py -m pipx ensurepath"
        uv_cmd = (
            'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        )
        python_cmd = "winget install --id Python.Python.3.12"
    else:  # pragma: no cover -- platform.system() only returns the three above in practice
        os_name = system.lower()
        pipx_cmd = uv_cmd = python_cmd = None

    checks["pipx"] = _which_check("pipx")
    checks["uv"] = _which_check("uv")
    if not checks["python"]["ok"]:
        checks["python"]["install_cmd"] = python_cmd
    if not checks["pipx"]["ok"]:
        checks["pipx"]["install_cmd"] = pipx_cmd
    if not checks["uv"]["ok"]:
        checks["uv"]["install_cmd"] = uv_cmd

    return {
        "os": os_name,
        "python_executable": sys.executable,
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks.values() if "ok" in c),
    }


@mcp.prompt()
def parapet_getting_started() -> str:
    """First-run menu for a newly connected Parapet MCP server -- surfaced
    as a pickable starter prompt by clients that list MCP prompts (e.g.
    Claude Code's /mcp menu). Not something this server can force onto the
    very first turn of a conversation -- no MCP mechanism fires a prompt
    automatically on connect -- so a client/skill that wants "ask before
    doing anything" behavior should invoke this explicitly as its first
    move, rather than assuming it already ran."""
    return (
        "Parapet is connected. What would you like to do?\n\n"
        "1. Build an example governed-agent app -- a runnable demo with two "
        "identities in different orgs (e.g. a Sales-org user and an HR-org "
        "user) showing Cedar allow one org's tool and deny the other's, for "
        "either Google ADK or Microsoft Agent Framework. (Use the "
        "parapet-quickdemo skill for this.)\n"
        "2. Set up prerequisites -- check for Python 3.12+/pipx/uv and "
        "install whatever's missing, one approved step at a time. (Use the "
        "parapet-install-prereqs skill for this.)\n"
        "3. Audit an existing codebase for ungoverned model/tool calls -- a "
        "local, static scan (no control plane call) scored high/medium/low, "
        "saved as a Markdown report, with a follow-up fix pass that wraps "
        "flagged sites in GovernedAgent/GovernedRunner. (Use the "
        "parapet-audit skill, then parapet-audit-fix, for this.)\n"
        "4. Something else -- available tools: parapet_login (device-code "
        "auth), parapet_whoami (who you are + your existing agents), "
        "parapet_provision_agent (create a new governed agent), "
        "parapet_get_quickstart (this deployment's install command / env "
        "vars / default model), parapet_list_agents, "
        "parapet_push_policy_file (write a Cedar policy into an agent's "
        "bundle), parapet_check_prerequisites (local Python/pipx/uv "
        "check), and parapet_audit_codebase (local static governance scan)."
        "\n\n"
        "Which would you like?"
    )


def main() -> None:
    mcp.run(transport="stdio")
