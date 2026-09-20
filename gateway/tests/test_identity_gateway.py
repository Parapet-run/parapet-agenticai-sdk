"""The gateway, end to end, with a verified identity method configured.

The properties that matter, each pinned below:

* the agent is whoever the BINDING says, never whoever the URL says;
* a bad credential is refused, and never downgrades to the path claim;
* the identity token is for the gateway and is never forwarded upstream;
* the verified claims reach Cedar, so role-gated policy works through a proxy.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import parapetai_gateway.server.app as app_module
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from parapetai_gateway.identity.bindings import Binding, BindingTable
from parapetai_gateway.identity.jwks import StaticKeyProvider
from parapetai_gateway.identity.resolver import IdentityResolver, IdpConfig, JwtVerifier
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

from .identity_support import AUDIENCE, ISSUER, make_token, new_rsa_key

POLICIES = Path(__file__).resolve().parents[2] / "policies"
KEY = new_rsa_key()
HEADER = "x-parapetai-identity"
_DENIED_TOOL = "execute_shell"  # denied by policies/20-tools.cedar
_ALLOWED_TOOL = "lookup_order"


def _resolver(*, require: bool = False) -> IdentityResolver:
    return IdentityResolver(
        bindings=BindingTable([Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1")]),
        jwt_verifier=JwtVerifier(
            IdpConfig(issuer=ISSUER, audiences=(AUDIENCE,)),
            StaticKeyProvider({"k1": KEY.public_key()}),
        ),
        require_verified=require,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch, *, require: bool = False, **overrides: Any
) -> TestClient:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, **overrides)
    )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine, identity_resolver=_resolver(require=require)))


def _call(
    client: TestClient,
    *,
    token: str | None = None,
    path: str = "/mcp",
    tool: str = _DENIED_TOOL,
    **headers: str,
) -> Response:
    if token is not None:
        headers[HEADER] = token
    return client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
        headers=headers,
    )


def _observed(client: TestClient) -> dict[str, Any]:
    records = client.get("/__parapetai/observations").json()["records"]
    assert records, "the request never reached the gateway's decision path"
    return records[0]


# ── the agent is whoever the binding says ────────────────────────────────


def test_the_verified_agent_is_the_bound_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    resp = _call(client, token=f"Bearer {make_token(KEY)}")

    assert resp.status_code == 403  # denied by Cedar policy, i.e. it got as far as a decision
    assert resp.headers["x-parapetai-decision"] == "deny"
    record = _observed(client)
    assert record["agent_id"] == "agent-a"
    assert record["identity_method"] == "jwt"


def test_cedar_is_evaluated_for_the_bound_agent_principal(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _call(_client(monkeypatch), token=make_token(KEY))

    assert audited[0]["principal"] == 'Agent::"agent-a"'
    assert audited[0]["context"]["identity_method"] == "jwt"


def test_a_bare_token_without_the_bearer_prefix_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    _call(client, token=make_token(KEY))

    assert _observed(client)["agent_id"] == "agent-a"


def test_a_url_naming_a_different_agent_than_the_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _call(_client(monkeypatch), token=make_token(KEY), path="/a/agent-b/mcp")

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "identity_path_mismatch"


def test_a_url_naming_the_same_agent_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)

    _call(client, token=make_token(KEY), path="/a/agent-a/mcp")

    assert _observed(client)["agent_id"] == "agent-a"


# ── a bad credential is refused and never downgrades ─────────────────────


@pytest.mark.parametrize("require", [False, True])
@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not-a-jwt", id="garbage"),
        pytest.param(make_token(KEY, claims={"exp": 1}), id="expired"),
        pytest.param(make_token(new_rsa_key()), id="forged-signature"),
        pytest.param(make_token(KEY, claims={"aud": "api://other"}), id="wrong-audience"),
    ],
)
def test_an_invalid_token_is_401_and_never_falls_back_to_the_path_claim(
    monkeypatch: pytest.MonkeyPatch, token: str, require: bool
) -> None:
    client = _client(monkeypatch, require=require)

    # The URL claims a perfectly good agent. A weaker method must not rescue a
    # request whose stronger credential failed.
    resp = _call(client, token=token, path="/a/agent-a/mcp")

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "invalid_token"
    assert resp.headers["www-authenticate"] == "Bearer"
    assert client.get("/__parapetai/observations").json()["records"] == []  # no decision made


def test_a_valid_token_for_an_unbound_identity_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _call(_client(monkeypatch), token=make_token(KEY, claims={"azp": "stranger-app"}))

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "identity_not_bound"


def test_the_refusal_does_not_say_why_the_token_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _call(_client(monkeypatch), token=make_token(KEY, claims={"exp": 1})).text

    assert "expired" not in body.lower()
    assert "signature" not in body.lower()


# ── no credential ────────────────────────────────────────────────────────


def test_no_credential_keeps_the_path_claim_behaviour_when_verification_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    _call(client, path="/a/some-agent/mcp")

    record = _observed(client)
    assert record["agent_id"] == "some-agent"
    assert record["identity_method"] == "path"


def test_no_credential_is_refused_when_verified_identity_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, require=True)

    resp = _call(client, path="/a/some-agent/mcp")

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "identity_required"
    assert client.get("/__parapetai/observations").json()["records"] == []


def test_the_gateways_own_control_endpoints_do_not_need_an_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, require=True)

    assert client.get("/__parapetai/health").status_code == 200


# ── claims reach Cedar ───────────────────────────────────────────────────


def test_verified_claims_and_roles_reach_the_decision_context(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    token = make_token(KEY, claims={"oid": "user-1", "roles": ["OrderViewer"]})

    _call(_client(monkeypatch), token=token)

    context = audited[0]["context"]
    assert context["identity_claims"]["oid"] == "user-1"
    assert context["identity_roles"] == ["OrderViewer"]
    assert context["agent_identity_claims"] == {"client_id": "app-1"}


def test_a_path_only_request_carries_no_identity_claims(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _call(_client(monkeypatch), path="/a/some-agent/mcp")

    context = audited[0]["context"]
    assert "identity_claims" not in context
    assert context["identity_method"] == "path"


@respx.mock
def test_a_role_gated_policy_now_works_through_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policies/30-identity.cedar forbids `lookup_order` unless the caller
    asserts the OrderViewer role. Before verified identity the HTTP path could
    never assert one; now the role comes from the verified token."""
    respx.post("https://jira.internal/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    )
    client = _client(monkeypatch, mcp_upstreams={"jira": "https://jira.internal/mcp"})

    without = _call(client, token=make_token(KEY), path="/mcp/jira", tool=_ALLOWED_TOOL)
    wrong = _call(
        client,
        token=make_token(KEY, claims={"roles": ["Intern"]}),
        path="/mcp/jira",
        tool=_ALLOWED_TOOL,
    )
    right = _call(
        client,
        token=make_token(KEY, claims={"roles": ["OrderViewer"]}),
        path="/mcp/jira",
        tool=_ALLOWED_TOOL,
    )

    assert without.status_code == 403  # asserted identity, no roles: denied
    assert wrong.status_code == 403
    assert right.status_code == 200


# ── the token is for the gateway, not the upstream ───────────────────────


@respx.mock
def test_the_identity_token_is_never_forwarded_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = respx.post("https://jira.internal/mcp").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    )
    client = _client(monkeypatch, mcp_upstreams={"jira": "https://jira.internal/mcp"})

    resp = _call(
        client,
        token=f"Bearer {make_token(KEY, claims={'roles': ['OrderViewer']})}",
        path="/mcp/jira",
        tool=_ALLOWED_TOOL,
        authorization="Bearer the-callers-own-jira-token",
    )

    assert resp.status_code == 200
    forwarded = upstream.calls.last.request.headers
    assert HEADER not in forwarded
    # ...while the caller's real downstream credential still rides through
    # untouched (passthrough mode), because the identity token used a header of
    # its own instead of Authorization.
    assert forwarded["authorization"] == "Bearer the-callers-own-jira-token"


# ── startup ──────────────────────────────────────────────────────────────


def test_identity_on_the_authorization_header_conflicts_with_mcp_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="conflicts with"):
        _client(
            monkeypatch,
            identity_header="authorization",
            mcp_auth_mode="oauth2",
            mcp_oauth_shared_secret="s3cret",  # noqa: S106
        )


def test_without_a_configured_method_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(
            app_module.settings,
            idp_issuer=None,
            idp_jwks_url=None,
            idp_audiences=(),
            tls_client_ca=None,
            require_verified_identity=False,
        ),
    )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    resp = _call(client, path="/a/whoever/mcp", **{HEADER: "ignored-garbage"})

    # No resolver -> the header means nothing and the path claim rules, as it
    # always did.
    assert resp.status_code == 403
    assert client.get("/__parapetai/observations").json()["records"][0]["agent_id"] == "whoever"
