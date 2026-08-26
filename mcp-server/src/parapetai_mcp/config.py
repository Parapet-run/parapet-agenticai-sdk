"""Local credential storage for parapetai-mcp -- one cli_token per control
plane, keyed by URL so a developer working against a local `make dev`
instance and a real deployment never collide. This file (~/.parapet/
credentials.json, mode 0600) is the ONLY durable copy of a cli_token
anywhere on this machine; the control plane itself only ever stores its
hash (control-plane/src/parapetai_control/cli_auth.py). The token is
written here directly by the MCP server process (server.py's
parapet_login) and never round-trips through an MCP tool result, so it
never lands in the model's context after the first mint.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(os.environ.get("PARAPETAI_MCP_CONFIG_DIR", str(Path.home() / ".parapet")))
_CREDENTIALS_PATH = _CONFIG_DIR / "credentials.json"

DEFAULT_CONTROL_PLANE_URL = os.environ.get(
    "PARAPETAI_CONTROL_PLANE_URL", "http://localhost:8090"
)


def _load() -> dict[str, Any]:
    if not _CREDENTIALS_PATH.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(_CREDENTIALS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt/unreadable file is treated as "not logged in yet", not
        # a crash -- parapet_login overwrites it cleanly on the next run.
        return {}
    return data


def get_cli_token(control_plane_url: str) -> str | None:
    creds = _load().get(control_plane_url.rstrip("/"))
    return creds.get("cli_token") if creds else None


def set_cli_token(control_plane_url: str, cli_token: str, *, account_id: str) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _load()
    data[control_plane_url.rstrip("/")] = {"cli_token": cli_token, "account_id": account_id}
    _CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    # 0600 -- this file holds a live bearer credential, same posture the
    # control plane itself uses for every secret it ever hands out once.
    _CREDENTIALS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def clear_cli_token(control_plane_url: str) -> None:
    data = _load()
    data.pop(control_plane_url.rstrip("/"), None)
    _CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
