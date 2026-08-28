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
import webbrowser
from typing import Any

from mcp.server.fastmcp import FastMCP

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
        "2. Something else -- available tools: parapet_login (device-code "
        "auth), parapet_whoami (who you are + your existing agents), "
        "parapet_provision_agent (create a new governed agent), "
        "parapet_get_quickstart (this deployment's install command / env "
        "vars / default model), parapet_list_agents, and "
        "parapet_push_policy_file (write a Cedar policy into an agent's "
        "bundle).\n\n"
        "Which would you like -- build the example app, or something else?"
    )


def main() -> None:
    mcp.run(transport="stdio")
