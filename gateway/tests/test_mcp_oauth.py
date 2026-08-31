"""OAuth 2.1 authorization_code+PKCE + Dynamic Client Registration in front
of /mcp (mcp_oauth.py) -- required by MCP clients that enforce the MCP
Authorization spec against a remote server they don't operate themselves
(e.g. Atlassian Rovo's external MCP server requirements). Disabled by
default (PARAPETAI_MCP_AUTH_MODE=none): those tests prove the existing,
unauthenticated /mcp path is untouched.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import parapetai_gateway.server.app as app_module
import pytest
from fastapi.testclient import TestClient
from parapetai_gateway import mcp_oauth
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"


@pytest.fixture(autouse=True)
def _reset_oauth_state() -> None:
    mcp_oauth.reset_state_for_tests()


def _tools_call(tool_name: str) -> dict[str, object]:
    params = {"name": tool_name, "arguments": {}}
    return {"jsonrpc": "2.0", "method": "tools/call", "params": params}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _oauth_client(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> TestClient:
    """Builds a fresh app with oauth2 mode enabled -- must happen AFTER
    patching settings, since create_app() decides route registration once,
    at construction time (same reason test_credential_forwarding.py builds
    its broker-mode app after monkeypatching, not via the shared fixture)."""
    defaults = {"mcp_auth_mode": "oauth2", "mcp_oauth_shared_secret": "testsecret"}
    merged = {**defaults, **overrides}
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, **merged))
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine))


# ── default (disabled) behaviour is unchanged ───────────────────────────


def test_mcp_reachable_without_bearer_when_oauth_disabled() -> None:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    resp = client.post("/a/probe/mcp", json=_tools_call("lookup_order"))
    # No upstream configured in this test env -- the point is it is NOT a 401
    # from the oauth gate, since that gate doesn't exist in "none" mode.
    assert resp.status_code != 401


def test_well_known_routes_absent_when_oauth_disabled() -> None:
    # No dedicated route registered -- falls through to the generic proxy
    # catch-all as an unrecognised "unknown" provider path (no upstream
    # configured for it), not a real oauth-authorization-server response.
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 502
    assert "issuer" not in resp.json()


# ── fail-closed startup ──────────────────────────────────────────────────


def test_oauth2_mode_without_shared_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = dataclasses.replace(
        app_module.settings, mcp_auth_mode="oauth2", mcp_oauth_shared_secret=None
    )
    monkeypatch.setattr(app_module, "settings", patched)
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    with pytest.raises(RuntimeError, match="PARAPETAI_MCP_OAUTH_SHARED_SECRET"):
        create_app(engine)


# ── metadata ─────────────────────────────────────────────────────────────


def test_metadata_endpoints_describe_this_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)

    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"].endswith("/mcp")
    authz_servers = resource["authorization_servers"]

    server_meta = client.get("/.well-known/oauth-authorization-server").json()
    assert server_meta["issuer"] in authz_servers
    assert server_meta["registration_endpoint"].endswith("/register")
    assert server_meta["code_challenge_methods_supported"] == ["S256"]


# ── the full DCR -> authorize -> token -> call flow ─────────────────────


def test_full_oauth_flow_then_mcp_call_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)

    reg = client.post("/register", json={"redirect_uris": ["https://rovo.example/callback"]})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce_pair()
    authorize = client.post(
        "/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "https://rovo.example/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "secret": "testsecret",
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    location = urlparse(authorize.headers["location"])
    qs = parse_qs(location.query)
    assert qs["state"] == ["xyz"]
    code = qs["code"][0]

    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://rovo.example/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200
    token_body = token_resp.json()
    access_token = token_body["access_token"]
    assert token_body["token_type"] == "Bearer"  # noqa: S105 -- OAuth field name, not a secret

    # Without the token: 401, with the MCP-auth-spec discovery hint.
    unauth = client.post("/a/rovo-agent/mcp", json=_tools_call("lookup_order"))
    assert unauth.status_code == 401
    assert "oauth-protected-resource" in unauth.headers["www-authenticate"]

    # With it: past the oauth gate (Cedar/upstream decide from here -- not a 401).
    authed = client.post(
        "/a/rovo-agent/mcp",
        json=_tools_call("lookup_order"),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert authed.status_code != 401


def test_authorize_rejects_wrong_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)
    reg = client.post("/register", json={"redirect_uris": ["https://rovo.example/callback"]})
    client_id = reg.json()["client_id"]
    _, challenge = _pkce_pair()

    resp = client.post(
        "/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "https://rovo.example/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "secret": "wrong",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200  # re-rendered form, not a redirect
    assert "Incorrect secret" in resp.text


def test_token_exchange_rejects_wrong_pkce_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)
    reg = client.post("/register", json={"redirect_uris": ["https://rovo.example/callback"]})
    client_id = reg.json()["client_id"]
    _verifier, challenge = _pkce_pair()

    authorize = client.post(
        "/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "https://rovo.example/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "secret": "testsecret",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(authorize.headers["location"]).query)["code"][0]

    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://rovo.example/callback",
            "client_id": client_id,
            "code_verifier": "not-the-real-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_authorize_rejects_unregistered_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)
    reg = client.post("/register", json={"redirect_uris": ["https://rovo.example/callback"]})
    client_id = reg.json()["client_id"]
    _, challenge = _pkce_pair()

    resp = client.post(
        "/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "secret": "testsecret",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_register_requires_redirect_uris(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _oauth_client(monkeypatch)
    resp = client.post("/register", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"
