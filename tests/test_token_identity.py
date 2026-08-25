"""Tests for parapetai_agent.token_identity -- pure stdlib, no agent_framework
dependency, so unlike test_maf.py this runs in the base `make test` suite,
not gated behind pytest.importorskip("agent_framework")."""

from __future__ import annotations

import base64
import json

from parapetai_agent.token_identity import (
    JwtIdentityExtractor,
    agent_identity_from_claims,
    decode_jwt_claims,
    identity_from_claims,
)


def _make_jwt(payload: dict) -> str:
    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.fakesignature"


class TestDecodeJwtClaims:
    def test_decodes_a_real_shaped_jwt(self) -> None:
        token = _make_jwt({"oid": "bob-oid", "roles": ["OrderViewer"]})
        assert decode_jwt_claims(token) == {"oid": "bob-oid", "roles": ["OrderViewer"]}

    def test_not_a_jwt_returns_none_not_an_error(self) -> None:
        assert decode_jwt_claims("not-a-jwt-at-all") is None
        assert decode_jwt_claims("") is None
        assert decode_jwt_claims("a.b") is None  # only 2 segments

    def test_malformed_payload_segment_returns_none(self) -> None:
        assert decode_jwt_claims("header.not-valid-base64!!!.sig") is None

    def test_non_object_payload_returns_none(self) -> None:
        # A JWT whose payload segment decodes to a JSON array, not an object.
        payload = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode().rstrip("=")
        assert decode_jwt_claims(f"header.{payload}.sig") is None


class TestIdentityFromClaims:
    def test_extracts_standard_and_entra_claim_names(self) -> None:
        claims = {
            "oid": "bob-oid",
            "tid": "tenant-1",
            "preferred_username": "bob@contoso.com",
            "roles": ["OrderViewer", "Admin"],
            "irrelevant_claim": "ignored",
        }
        end_user_claims, roles = identity_from_claims(claims)
        assert end_user_claims == {
            "oid": "bob-oid",
            "tid": "tenant-1",
            "preferred_username": "bob@contoso.com",
        }
        assert roles == ["OrderViewer", "Admin"]

    def test_falls_back_to_groups_when_roles_absent(self) -> None:
        _, roles = identity_from_claims({"oid": "x", "groups": ["group-a", "group-b"]})
        assert roles == ["group-a", "group-b"]

    def test_no_roles_or_groups_is_empty_not_an_error(self) -> None:
        _, roles = identity_from_claims({"oid": "x"})
        assert roles == []


class TestAgentIdentityFromClaims:
    def test_rfc8693_act_claim_wins(self) -> None:
        claims = {"oid": "bob", "act": {"sub": "agent-client-id"}, "azp": "should-not-be-used"}
        agent = agent_identity_from_claims(claims)
        assert agent is not None
        assert agent[0] == {"sub": "agent-client-id"}

    def test_azp_fallback_when_no_act_claim(self) -> None:
        agent = agent_identity_from_claims({"oid": "bob", "azp": "agent-app-id"})
        assert agent == ({"client_id": "agent-app-id"}, [])

    def test_appid_fallback_when_no_act_or_azp(self) -> None:
        agent = agent_identity_from_claims({"oid": "bob", "appid": "legacy-agent-app-id"})
        assert agent == ({"client_id": "legacy-agent-app-id"}, [])

    def test_no_delegation_signal_returns_none_not_empty(self) -> None:
        """None, not ({}, []) -- a plain end-user token with no delegation
        is the common case, not an error, and the caller (JwtIdentityExtractor)
        needs to distinguish it from "delegation present but empty"."""
        assert agent_identity_from_claims({"oid": "bob", "preferred_username": "bob@x.com"}) is None

    def test_empty_act_object_is_treated_as_absent(self) -> None:
        assert agent_identity_from_claims({"oid": "bob", "act": {}}) is None


class TestJwtIdentityExtractor:
    def test_full_extraction_with_delegation(self) -> None:
        token = _make_jwt(
            {
                "oid": "bob-oid",
                "preferred_username": "bob@contoso.com",
                "roles": ["OrderViewer"],
                "act": {"sub": "agent-sp-client-id"},
            }
        )
        identity = JwtIdentityExtractor().extract(token)
        assert identity.end_user_claims == {
            "oid": "bob-oid",
            "preferred_username": "bob@contoso.com",
        }
        assert identity.end_user_roles == ["OrderViewer"]
        assert identity.agent_claims == {"sub": "agent-sp-client-id"}
        assert identity.agent_roles == []

    def test_full_extraction_without_delegation(self) -> None:
        token = _make_jwt({"oid": "bob-oid", "roles": ["OrderViewer"]})
        identity = JwtIdentityExtractor().extract(token)
        assert identity.end_user_claims == {"oid": "bob-oid"}
        assert identity.end_user_roles == ["OrderViewer"]
        assert identity.agent_claims == {}
        assert identity.agent_roles == []

    def test_undecodable_token_yields_fully_empty_identity(self) -> None:
        identity = JwtIdentityExtractor().extract("not-a-jwt")
        assert identity.end_user_claims == {}
        assert identity.end_user_roles == []
        assert identity.agent_claims == {}
        assert identity.agent_roles == []
