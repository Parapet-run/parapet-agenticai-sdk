"""Thin httpx wrapper over the control plane's CLI API
(control-plane/src/parapetai_control/cli_api.py). No business logic lives
here -- every method is a direct call to one endpoint; provisioning,
device-code minting, etc. are all implemented control-plane-side and
reused, not duplicated.
"""

from __future__ import annotations

from typing import Any

import httpx

from parapetai_mcp.config import get_cli_token


class NotLoggedInError(RuntimeError):
    def __init__(self, control_plane_url: str) -> None:
        super().__init__(
            f"not logged in to {control_plane_url} -- run the parapet_login tool first"
        )


class ControlPlaneClient:
    def __init__(self, control_plane_url: str) -> None:
        self.control_plane_url = control_plane_url.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        token = get_cli_token(self.control_plane_url)
        if token is None:
            raise NotLoggedInError(self.control_plane_url)
        return {"Authorization": f"Bearer {token}"}

    async def start_device_code(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.post("/api/v1/cli/device_code")
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def poll_device_code(self, device_code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.post(
                "/api/v1/cli/device_code/poll", json={"device_code": device_code}
            )
            if resp.status_code == 410:
                # Expired/consumed -- the caller (server.py's parapet_login
                # loop) treats this as "give up", not "retry forever".
                result: dict[str, Any] = {"status": "gone"}
                return result
            resp.raise_for_status()
            result = resp.json()
            return result

    async def whoami(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.get("/api/v1/cli/whoami", headers=self._auth_headers())
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_quickstart(self) -> dict[str, str]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.get("/api/v1/cli/quickstart")
            resp.raise_for_status()
            result: dict[str, str] = resp.json()
            return result

    async def provision_agent(
        self, *, display_name: str | None = None, tenant_id: str | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.post(
                "/api/v1/cli/agents",
                json={"display_name": display_name, "tenant_id": tenant_id},
                headers=self._auth_headers(),
            )
            if resp.status_code == 403:
                raise PermissionError(resp.json().get("detail", "not permitted to provision"))
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def push_bundle_file(self, agent_id: str, filename: str, content: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.control_plane_url, timeout=10) as http:
            resp = await http.post(
                f"/api/v1/cli/agents/{agent_id}/bundle",
                json={"filename": filename, "content": content},
                headers=self._auth_headers(),
            )
            if resp.status_code == 403:
                raise PermissionError(resp.json().get("detail", "not permitted to edit bundles"))
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
