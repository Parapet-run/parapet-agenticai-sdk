"""Tests for parapetai_agent.token_identity -- pure stdlib, no agent_framework
dependency, so unlike test_maf.py this runs in the base `make test` suite,
not gated behind pytest.importorskip("agent_framework")."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from parapetai_agent.token_identity import (
    BackgroundOpaqueTokenResolver,
    JwtIdentityExtractor,
    OpaqueTokenIntrospector,
    _normalize_introspection_claims,
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

    def test_aud_is_extracted_as_a_registered_jwt_claim(self) -> None:
        end_user_claims, _ = identity_from_claims({"oid": "x", "aud": "api://salesforce-mcp"})
        assert end_user_claims["aud"] == "api://salesforce-mcp"

    def test_aud_as_a_json_array_is_comma_joined_not_python_repr(self) -> None:
        # RFC 7519 §4.1.3: aud MAY be a JSON array of strings (a token
        # issued for more than one audience) -- must not come back as
        # Python's list repr ("['a', 'b']").
        end_user_claims, _ = identity_from_claims(
            {"oid": "x", "aud": ["api://salesforce-mcp", "api://docs-mcp"]}
        )
        assert end_user_claims["aud"] == "api://salesforce-mcp, api://docs-mcp"

    def test_resource_is_never_extracted_from_claims(self) -> None:
        # RFC 8707/RFC 9728: `resource` is a request-time parameter and the
        # protected resource's own published identifier, not a claim any
        # spec guarantees is echoed into the token -- this module must not
        # invent one.
        end_user_claims, _ = identity_from_claims(
            {"oid": "x", "resource": "https://mcp.salesforce.internal/v1"}
        )
        assert "resource" not in end_user_claims


class TestAgentIdentityFromClaims:
    def test_rfc8693_act_claim_wins(self) -> None:
        claims = {"oid": "bob", "act": {"sub": "agent-client-id"}, "azp": "should-not-be-used"}
        agent = agent_identity_from_claims(claims)
        assert agent is not None
        assert agent[0] == {"sub": "agent-client-id"}

    def test_rfc9068_client_id_fallback_when_no_act_claim(self) -> None:
        # RFC 9068 §2.2's own REQUIRED claim for a JWT-formatted OAuth
        # access token -- the standard name for the same concept azp/appid
        # represent as OIDC/Entra-specific conventions. A real,
        # previously-shipped gap: this was never checked at all before,
        # only act/azp/appid.
        agent = agent_identity_from_claims({"sub": "bob", "client_id": "agent-app-id"})
        assert agent == ({"client_id": "agent-app-id"}, [])

    def test_client_id_wins_over_azp_when_both_present(self) -> None:
        # client_id checked before azp -- the RFC-standard name takes
        # priority when a token happens to carry both.
        agent = agent_identity_from_claims(
            {"sub": "bob", "client_id": "rfc9068-client", "azp": "should-not-win"}
        )
        assert agent == ({"client_id": "rfc9068-client"}, [])

    def test_azp_fallback_when_no_act_or_client_id(self) -> None:
        agent = agent_identity_from_claims({"oid": "bob", "azp": "agent-app-id"})
        assert agent == ({"client_id": "agent-app-id"}, [])

    def test_appid_fallback_when_no_act_client_id_or_azp(self) -> None:
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

    def test_rfc9068_access_token_shape_resolves_agent_identity_via_client_id(self) -> None:
        # A minimal RFC 9068-compliant JWT access token: iss/exp/aud/sub/
        # iat/jti (all §2.2 REQUIRED) plus client_id (also REQUIRED,
        # defined in RFC 8693 §4.3) -- no act, no azp, no appid. Before
        # the fix this resolved agent_claims == {} for every such token.
        token = _make_jwt(
            {
                "iss": "https://as.example.com/",
                "sub": "user-42",
                "aud": "https://api.example.com/",
                "client_id": "s6BhdRkqt3",
                "exp": 1893456000,
                "iat": 1893452400,
                "jti": "unique-jwt-id",
            }
        )
        identity = JwtIdentityExtractor().extract(token)
        assert identity.end_user_claims["sub"] == "user-42"
        assert identity.agent_claims == {"client_id": "s6BhdRkqt3"}

    def test_undecodable_token_yields_fully_empty_identity(self) -> None:
        identity = JwtIdentityExtractor().extract("not-a-jwt")
        assert identity.end_user_claims == {}
        assert identity.end_user_roles == []
        assert identity.agent_claims == {}
        assert identity.agent_roles == []

    def test_rfc7519_and_oidc_standard_claims_are_extracted(self) -> None:
        token = _make_jwt(
            {
                "oid": "bob-oid",
                "name": "Bob Smith",
                "given_name": "Bob",
                "family_name": "Smith",
                "iss": "https://login.contoso.com/tenant-1/v2.0",
                "aud": "api://salesforce-mcp",
                "exp": 1893456000,
                "iat": 1893452400,
                "nbf": 1893452400,
                "jti": "unique-token-id",
                "azp": "client-app-id",
                "amr": ["pwd", "mfa"],
                "scp": "Files.Read Mail.Send",
            }
        )
        identity = JwtIdentityExtractor().extract(token)
        assert identity.end_user_claims["name"] == "Bob Smith"
        assert identity.end_user_claims["given_name"] == "Bob"
        assert identity.end_user_claims["family_name"] == "Smith"
        assert identity.end_user_claims["iss"] == "https://login.contoso.com/tenant-1/v2.0"
        assert identity.end_user_claims["aud"] == "api://salesforce-mcp"
        assert identity.end_user_claims["exp"] == "1893456000"
        assert identity.end_user_claims["jti"] == "unique-token-id"
        assert identity.end_user_claims["azp"] == "client-app-id"
        assert identity.end_user_claims["amr"] == "pwd, mfa"
        assert identity.end_user_claims["scp"] == "Files.Read Mail.Send"

    def test_scope_claim_name_variant_is_also_extracted(self) -> None:
        # RFC 6749 §3.3 -- `scope`, not Entra's `scp` -- must be captured too.
        token = _make_jwt({"sub": "u1", "scope": "read write"})
        identity = JwtIdentityExtractor().extract(token)
        assert identity.end_user_claims["scope"] == "read write"

    def test_agent_act_claim_carries_its_own_issuer(self) -> None:
        # RFC 8693 §4.1: `act` typically carries `iss` alongside `sub` when
        # the actor was issued by a different IdP than the outer token.
        token = _make_jwt(
            {"oid": "bob", "act": {"sub": "agent-1", "iss": "https://agent-idp.example"}}
        )
        identity = JwtIdentityExtractor().extract(token)
        assert identity.agent_claims == {"sub": "agent-1", "iss": "https://agent-idp.example"}


class TestRfc7662Introspector:
    def test_active_token_returns_normalized_claims(self, monkeypatch: Any) -> None:
        import httpx

        from parapetai_agent.token_identity import Rfc7662Introspector

        captured: dict[str, Any] = {}

        def _fake_post(url: str, *, data: dict[str, Any], auth: Any, timeout: float) -> Any:
            captured["url"] = url
            captured["data"] = data
            captured["auth"] = auth
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "sub": "u1",
                    "username": "bob@contoso.com",
                    "client_id": "agent-app-id",
                    "scope": "read write",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", _fake_post)
        introspector = Rfc7662Introspector(
            introspection_endpoint="https://idp.example/introspect",
            client_id="rs-client",
            client_secret="rs-secret",  # noqa: S106 -- test fixture, not a real credential
        )
        claims = introspector.introspect("opaque-token-1")
        assert claims == {
            "active": True,
            "sub": "u1",
            "username": "bob@contoso.com",
            "preferred_username": "bob@contoso.com",  # normalized
            "client_id": "agent-app-id",  # already a standard name -- no mapping needed
            "scope": "read write",
        }
        assert captured["url"] == "https://idp.example/introspect"
        assert captured["data"] == {"token": "opaque-token-1"}
        assert captured["auth"] == ("rs-client", "rs-secret")

    def test_inactive_token_returns_none_per_rfc7662(self, monkeypatch: Any) -> None:
        import httpx

        from parapetai_agent.token_identity import Rfc7662Introspector

        monkeypatch.setattr(
            httpx,
            "post",
            lambda url, **k: httpx.Response(
                200, json={"active": False}, request=httpx.Request("POST", url)
            ),
        )
        introspector = Rfc7662Introspector("https://idp.example/introspect", "c", "s")
        assert introspector.introspect("revoked-token") is None

    def test_network_failure_returns_none_not_an_exception(self, monkeypatch: Any) -> None:
        import httpx

        from parapetai_agent.token_identity import Rfc7662Introspector

        def _raise(*a: Any, **k: Any) -> Any:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", _raise)
        introspector = Rfc7662Introspector("https://idp.example/introspect", "c", "s")
        assert introspector.introspect("any-token") is None


class TestOidcUserInfoIntrospector:
    def test_returns_the_userinfo_response_as_claims(self, monkeypatch: Any) -> None:
        import httpx

        from parapetai_agent.token_identity import OidcUserInfoIntrospector

        captured: dict[str, Any] = {}

        def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
            captured["url"] = url
            captured["headers"] = headers
            return httpx.Response(
                200,
                json={"sub": "u1", "email": "bob@contoso.com"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", _fake_get)
        introspector = OidcUserInfoIntrospector("https://idp.example/userinfo")
        claims = introspector.introspect("opaque-token-1")
        assert claims == {"sub": "u1", "email": "bob@contoso.com"}
        assert captured["url"] == "https://idp.example/userinfo"
        assert captured["headers"] == {"Authorization": "Bearer opaque-token-1"}


class TestNormalizeIntrospectionClaims:
    def test_username_maps_to_preferred_username(self) -> None:
        out = _normalize_introspection_claims({"sub": "u1", "username": "bob@contoso.com"})
        assert out["preferred_username"] == "bob@contoso.com"

    def test_client_id_needs_no_mapping_already_a_standard_name(self) -> None:
        # RFC 7662's own client_id IS RFC 9068's/RFC 8693's own name too --
        # agent_identity_from_claims() checks it directly, no normalization
        # needed (unlike username -> preferred_username above).
        out = _normalize_introspection_claims({"sub": "u1", "client_id": "agent-app-id"})
        assert out["client_id"] == "agent-app-id"
        assert "azp" not in out

    def test_does_not_overwrite_an_already_present_jwt_style_claim(self) -> None:
        out = _normalize_introspection_claims(
            {"preferred_username": "real@contoso.com", "username": "should-not-win"}
        )
        assert out["preferred_username"] == "real@contoso.com"


def _poll_until(predicate, timeout_s: float = 2.0, interval_s: float = 0.01) -> bool:  # type: ignore[no-untyped-def]
    """Polls `predicate()` until it's truthy or `timeout_s` elapses --
    avoids a flaky fixed-sleep wait for the background resolver thread."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


class _FakeIntrospector(OpaqueTokenIntrospector):
    """Returns a canned claims dict, optionally after an artificial delay
    (simulating real Authorization Server latency) -- constructed per test
    so each test's resolver is fully isolated from the others'."""

    def __init__(self, claims: dict[str, Any] | None, *, delay_s: float = 0.0) -> None:
        self._claims = claims
        self._delay_s = delay_s
        self.calls = 0

    def introspect(self, token: str) -> dict[str, Any] | None:
        self.calls += 1
        if self._delay_s:
            time.sleep(self._delay_s)
        return self._claims


class TestBackgroundOpaqueTokenResolver:
    def test_cache_miss_before_any_resolution(self) -> None:
        resolver = BackgroundOpaqueTokenResolver(_FakeIntrospector({"sub": "u1"}))
        assert resolver.get_cached("opaque-token-1") is None

    def test_request_resolution_never_blocks_even_with_a_slow_introspector(self) -> None:
        # The whole point: a request path calling this must never pay a
        # real Authorization Server's latency.
        introspector = _FakeIntrospector({"sub": "u1"}, delay_s=0.5)
        resolver = BackgroundOpaqueTokenResolver(introspector)
        started = time.monotonic()
        resolver.request_resolution("opaque-token-1")
        elapsed = time.monotonic() - started
        assert elapsed < 0.05

    def test_resolution_eventually_populates_the_cache(self) -> None:
        introspector = _FakeIntrospector({"sub": "u1", "preferred_username": "bob@contoso.com"})
        resolver = BackgroundOpaqueTokenResolver(introspector)
        resolver.request_resolution("opaque-token-1")
        assert _poll_until(lambda: resolver.get_cached("opaque-token-1") is not None)
        assert resolver.get_cached("opaque-token-1") == {
            "sub": "u1",
            "preferred_username": "bob@contoso.com",
        }

    def test_inactive_or_failed_introspection_is_not_cached(self) -> None:
        introspector = _FakeIntrospector(None)  # e.g. RFC 7662 active: false
        resolver = BackgroundOpaqueTokenResolver(introspector)
        resolver.request_resolution("opaque-token-1")
        assert _poll_until(lambda: introspector.calls > 0)
        assert resolver.get_cached("opaque-token-1") is None

    def test_duplicate_in_flight_requests_for_the_same_token_are_coalesced(self) -> None:
        introspector = _FakeIntrospector({"sub": "u1"}, delay_s=0.1)
        resolver = BackgroundOpaqueTokenResolver(introspector)
        resolver.request_resolution("opaque-token-1")
        resolver.request_resolution("opaque-token-1")  # while the first is still in flight
        resolver.request_resolution("opaque-token-1")
        assert _poll_until(lambda: resolver.get_cached("opaque-token-1") is not None)
        assert introspector.calls == 1

    def test_a_stale_entry_past_ttl_is_treated_as_a_miss(self) -> None:
        introspector = _FakeIntrospector({"sub": "u1"})
        resolver = BackgroundOpaqueTokenResolver(introspector, ttl_s=0.05)
        resolver.request_resolution("opaque-token-1")
        assert _poll_until(lambda: resolver.get_cached("opaque-token-1") is not None)
        time.sleep(0.1)
        assert resolver.get_cached("opaque-token-1") is None


class TestJwtIdentityExtractorWithOpaqueResolver:
    def test_first_call_with_an_opaque_token_resolves_empty_and_enqueues(self) -> None:
        introspector = _FakeIntrospector({"sub": "u1"})
        resolver = BackgroundOpaqueTokenResolver(introspector)
        identity = JwtIdentityExtractor(opaque_resolver=resolver).extract("opaque-token-xyz")
        assert identity.end_user_claims == {}
        assert identity.end_user_roles == []
        assert _poll_until(lambda: resolver.get_cached("opaque-token-xyz") is not None)

    def test_second_call_after_background_resolution_gets_real_identity(self) -> None:
        # _FakeIntrospector stands in for ANY OpaqueTokenIntrospector, so its
        # claims are already JWT-shaped (preferred_username, not RFC 7662's
        # `username`) -- normalization from RFC 7662's specific field names
        # is Rfc7662Introspector's own job, covered separately by
        # TestNormalizeIntrospectionClaims, not re-tested through the fake
        # here.
        introspector = _FakeIntrospector(
            {
                "sub": "u1",
                "preferred_username": "bob@contoso.com",
                "roles": ["OrderViewer"],
                "act": {"sub": "agent-1"},
            }
        )
        resolver = BackgroundOpaqueTokenResolver(introspector)
        extractor = JwtIdentityExtractor(opaque_resolver=resolver)

        first = extractor.extract("opaque-token-xyz")
        assert first.end_user_claims == {}

        assert _poll_until(lambda: resolver.get_cached("opaque-token-xyz") is not None)

        second = extractor.extract("opaque-token-xyz")
        assert second.end_user_claims["sub"] == "u1"
        assert second.end_user_claims["preferred_username"] == "bob@contoso.com"
        assert second.end_user_roles == ["OrderViewer"]
        # BOTH end-user and agent identity resolved from the SAME opaque-token
        # resolution -- not just the end-user half.
        assert second.agent_claims == {"sub": "agent-1"}

    def test_without_a_resolver_opaque_tokens_are_never_resolved(self) -> None:
        # Default behavior, unchanged -- opaque_resolver is opt-in.
        identity = JwtIdentityExtractor().extract("opaque-token-xyz")
        assert identity.end_user_claims == {}
        assert identity.agent_claims == {}
