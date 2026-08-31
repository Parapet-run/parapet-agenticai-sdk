"""The gateway is a reverse proxy for an ARBITRARY set of downstream MCP
servers, not one fixed one. /mcp (PARAPETAI_MCP_BASE_URL) keeps working
unchanged for a single-target deployment; /mcp/{target} routes to whichever
server PARAPETAI_MCP_UPSTREAMS names `target` as -- see
config.Settings.mcp_upstream_for and server/app.py's _mcp_target.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import parapetai_gateway.server.app as app_module
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"

_TOOL_CALL = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "lookup_order", "arguments": {}},
}


def _client_with_upstreams(
    monkeypatch: pytest.MonkeyPatch, upstreams: dict[str, str], policies: Path | None = None
) -> TestClient:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, mcp_upstreams=upstreams)
    )
    policy_dir = policies or POLICIES
    entities = None if policies else POLICIES / "entities.json"
    engine = PolicyEngine(policy_dir, entities)
    return TestClient(create_app(engine))


@respx.mock
def test_named_target_routes_to_its_own_complete_upstream_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.post("https://jira-mcp.internal:9000/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "result": {}})
    )
    client = _client_with_upstreams(monkeypatch, {"jira": "https://jira-mcp.internal:9000/mcp"})

    resp = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)

    assert resp.status_code == 200
    assert route.called
    # The /mcp/jira routing prefix is Parapet-only -- the real server sees
    # exactly the URL configured for it, nothing appended.
    assert route.calls.last.request.url == "https://jira-mcp.internal:9000/mcp"


@respx.mock
def test_two_targets_route_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    jira = respx.post("https://jira.example/mcp")
    jira.mock(return_value=Response(200, json={"ok": "jira"}))
    gh = respx.post("https://gh.example/mcp")
    gh.mock(return_value=Response(200, json={"ok": "gh"}))
    client = _client_with_upstreams(
        monkeypatch, {"jira": "https://jira.example/mcp", "github": "https://gh.example/mcp"}
    )

    client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
    client.post("/a/probe/mcp/github", json=_TOOL_CALL)

    assert jira.called
    assert gh.called


def test_unrecognised_target_502s_even_with_a_default_upstream_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A configured single-target default must NOT be a silent fallback for a
    # target name that isn't in PARAPETAI_MCP_UPSTREAMS -- that would route a
    # caller who asked for "servicenow" at whatever bare /mcp happens to be.
    monkeypatch.setenv("PARAPETAI_MCP_BASE_URL", "https://default.example")
    client = _client_with_upstreams(monkeypatch, {"jira": "https://jira.example/mcp"})

    resp = client.post("/a/probe/mcp/servicenow", json=_TOOL_CALL)

    assert resp.status_code == 502
    assert "servicenow" in resp.json()["error"]["message"]


def test_bare_mcp_path_unaffected_by_configured_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    # /mcp (no target segment) keeps using PARAPETAI_MCP_BASE_URL exactly as
    # before this feature existed, even when named targets are configured.
    monkeypatch.delenv("PARAPETAI_MCP_BASE_URL", raising=False)
    client = _client_with_upstreams(monkeypatch, {"jira": "https://jira.example/mcp"})

    resp = client.post("/a/probe/mcp", json=_TOOL_CALL)

    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "no upstream configured for mcp"


def test_mcp_target_is_visible_to_cedar_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "00-base.cedar").write_text(
        'permit(principal, action == Action::"tool_call", resource);'
        'permit(principal, action == Action::"model_call", resource);'
    )
    (tmp_path / "10-block-servicenow.cedar").write_text(
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has mcp_target && context.mcp_target == "servicenow" };'
    )
    client = _client_with_upstreams(
        monkeypatch,
        {"jira": "https://jira.example/mcp", "servicenow": "https://sn.example/mcp"},
        policies=tmp_path,
    )

    with respx.mock:
        respx.post("https://jira.example/mcp").mock(return_value=Response(200, json={}))
        allowed = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)
        denied = client.post("/a/probe/mcp/servicenow", json=_TOOL_CALL)

    assert allowed.status_code == 200
    assert denied.status_code == 403


def test_protected_resource_metadata_is_path_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(
            app_module.settings,
            mcp_auth_mode="oauth2",
            mcp_oauth_shared_secret="s",  # noqa: S106 -- fixture value, not a real credential
            mcp_upstreams={"jira": "https://jira.example/mcp"},
        ),
    )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    bare = client.get("/.well-known/oauth-protected-resource").json()
    jira = client.get("/.well-known/oauth-protected-resource/a/probe/mcp/jira").json()

    assert bare["resource"].endswith("/mcp")
    assert jira["resource"].endswith("/a/probe/mcp/jira")
    assert bare["resource"] != jira["resource"]


def test_401_points_at_the_target_specific_metadata_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(
            app_module.settings,
            mcp_auth_mode="oauth2",
            mcp_oauth_shared_secret="s",  # noqa: S106 -- fixture value, not a real credential
            mcp_upstreams={"jira": "https://jira.example/mcp"},
        ),
    )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    resp = client.post("/a/probe/mcp/jira", json=_TOOL_CALL)

    assert resp.status_code == 401
    # Built from the post-identity-stripped `path` (same convention as the
    # audit context's own "path" field) -- the /a/{agent_id} prefix is a
    # caller identity claim, not part of the resource's own identifier.
    assert "/mcp/jira" in resp.headers["www-authenticate"]
