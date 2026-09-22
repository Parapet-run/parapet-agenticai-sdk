"""Verified caller identity: token verification, key sourcing, bindings and
startup validation.

Every "refuses" case here is a fail-closed proof (CONTRIBUTING.md): the
interesting question about an identity check is never "does a good token pass"
but "does every kind of bad one fail".
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from parapetai_gateway.config import Settings
from parapetai_gateway.identity.bindings import Binding, BindingError, BindingTable
from parapetai_gateway.identity.jwks import JwksKeyProvider, StaticKeyProvider
from parapetai_gateway.identity.resolver import (
    IdentityError,
    IdentityResolver,
    IdpConfig,
    JwtVerifier,
    build_resolver,
    token_from_header,
)

from parapetai_agent.agent_secrets import generate_secret, hash_secret

from .identity_support import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    hs256_confusion_token,
    jwk_for,
    make_token,
    new_rsa_key,
)

KEY = new_rsa_key()
OTHER_KEY = new_rsa_key()


def _verifier(**config: Any) -> JwtVerifier:
    cfg = IdpConfig(issuer=ISSUER, audiences=(AUDIENCE,), **config)
    return JwtVerifier(cfg, StaticKeyProvider({"k1": KEY.public_key()}))


def _resolver(**kwargs: Any) -> IdentityResolver:
    return IdentityResolver(
        bindings=BindingTable([Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1")]),
        jwt_verifier=_verifier(),
        **kwargs,
    )


# ── token verification ───────────────────────────────────────────────────


def test_a_valid_token_yields_its_claims() -> None:
    claims = _verifier().verify(make_token(KEY))

    assert claims["azp"] == "app-1"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(make_token(KEY, claims={"exp": 1}), id="expired"),
        pytest.param(make_token(KEY, claims={"aud": "api://someone-else"}), id="wrong-audience"),
        pytest.param(make_token(KEY, claims={"iss": "https://evil.example/"}), id="wrong-issuer"),
        pytest.param(make_token(KEY, drop=("exp",)), id="no-exp"),
        pytest.param(make_token(KEY, drop=("aud",)), id="no-aud"),
        pytest.param(make_token(KEY, drop=("iss",)), id="no-iss"),
        pytest.param(make_token(KEY, claims={"nbf": 4102444800}), id="not-yet-valid"),
        pytest.param(make_token(KEY, kid="unknown-kid"), id="unknown-kid"),
        pytest.param(make_token(KEY, kid=None), id="no-kid"),
        pytest.param(make_token(OTHER_KEY, kid="k1"), id="signed-by-a-different-key"),
        pytest.param(make_token(KEY, alg="RS512"), id="algorithm-not-allowlisted"),
        pytest.param(make_token(None, alg="none"), id="alg-none"),
        pytest.param(hs256_confusion_token(KEY, "k1"), id="hs256-with-public-key-as-secret"),
        pytest.param("not.a.jwt", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
def test_every_kind_of_bad_token_is_refused(token: str) -> None:
    with pytest.raises(IdentityError) as exc:
        _verifier().verify(token)

    assert exc.value.status == 401
    assert exc.value.code == "invalid_token"


def test_a_tampered_payload_fails_signature_verification() -> None:
    token = make_token(KEY)
    header, _, signature = token.split(".")
    forged_body = make_token(KEY, claims={"azp": "the-admin-app"}).split(".")[1]

    with pytest.raises(IdentityError):
        _verifier().verify(f"{header}.{forged_body}.{signature}")


@pytest.mark.parametrize("alg", ["HS256", "HS512", "none", "None"])
def test_a_symmetric_or_none_algorithm_cannot_even_be_configured(alg: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        IdpConfig(issuer=ISSUER, audiences=(AUDIENCE,), algorithms=(alg,))


def test_an_idp_config_needs_an_audience() -> None:
    with pytest.raises(ValueError, match="audience"):
        IdpConfig(issuer=ISSUER, audiences=())


def test_agent_claims_are_read_in_configured_order() -> None:
    verifier = _verifier()

    assert verifier.subject_of({"azp": "v2-app", "appid": "v1-app"}) == "v2-app"
    assert verifier.subject_of({"appid": "v1-app"}) == "v1-app"
    assert verifier.subject_of({"sub": "a-user"}) is None  # `sub` is not an agent claim by default


# ── resolver ─────────────────────────────────────────────────────────────


def test_a_bound_token_resolves_to_the_bound_agent_not_the_token_subject() -> None:
    identity = _resolver().resolve(peer_cert_der=None, identity_token=make_token(KEY))

    assert identity is not None
    assert identity.agent_id == "agent-a"
    assert identity.subject == "app-1"
    assert identity.method == "jwt"


def test_a_verified_but_unbound_token_is_forbidden_not_defaulted() -> None:
    with pytest.raises(IdentityError) as exc:
        _resolver().resolve(
            peer_cert_der=None, identity_token=make_token(KEY, claims={"azp": "some-other-app"})
        )

    assert (exc.value.status, exc.value.code) == (403, "identity_not_bound")


def test_a_token_with_no_agent_claim_is_refused() -> None:
    with pytest.raises(IdentityError) as exc:
        _resolver().resolve(
            peer_cert_der=None, identity_token=make_token(KEY, claims={"sub": "u"}, drop=("azp",))
        )

    assert exc.value.status == 401


def test_no_credential_is_not_an_error() -> None:
    assert _resolver().resolve(peer_cert_der=None, identity_token=None) is None


# ── shared-secret identity (additive to mTLS/JWT, off by default) ─────────


def _secret_resolver(**kwargs: Any) -> tuple[IdentityResolver, str]:
    secret = generate_secret()
    table = BindingTable([Binding("secret", "agent-a", secret_hash=hash_secret(secret))])
    resolver = IdentityResolver(bindings=table, allow_shared_secret=True, **kwargs)
    return resolver, secret


def test_a_bound_secret_resolves_to_the_bound_agent() -> None:
    resolver, secret = _secret_resolver()

    identity = resolver.resolve(peer_cert_der=None, identity_token=secret)

    assert identity is not None
    assert identity.agent_id == "agent-a"
    assert identity.method == "secret"


def test_a_wrong_secret_is_refused_as_invalid_not_unbound() -> None:
    resolver, _secret = _secret_resolver()

    with pytest.raises(IdentityError) as exc:
        resolver.resolve(
            peer_cert_der=None,
            identity_token="garbage-not-a-real-secret",  # noqa: S106 -- test fixture, not real
        )

    # No separate "verify, then check binding" step exists for a secret --
    # wrong and unbound are the same case, so this must be 401 (the
    # credential itself didn't verify), never 403 identity_not_bound, which
    # would wrongly imply the gateway recognized who was calling.
    assert (exc.value.status, exc.value.code) == (401, "invalid_secret")


def test_shared_secret_disabled_by_default_needs_an_explicit_flag() -> None:
    table = BindingTable([Binding("secret", "agent-a", secret_hash=hash_secret("s"))])
    with pytest.raises(ValueError, match="at least one identity method"):
        IdentityResolver(bindings=table)  # allow_shared_secret defaults to False


def test_a_jwt_shaped_header_is_never_reinterpreted_as_a_secret() -> None:
    # Both methods enabled on the same gateway: a well-formed JWT must go
    # through JWT verification, never be hashed and looked up as if it were
    # an opaque secret, even though shared-secret is also turned on.
    table = BindingTable(
        [
            Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1"),
            Binding("secret", "agent-b", secret_hash=hash_secret("s")),
        ]
    )
    resolver = IdentityResolver(
        bindings=table, jwt_verifier=_verifier(), allow_shared_secret=True
    )

    identity = resolver.resolve(peer_cert_der=None, identity_token=make_token(KEY))

    assert identity is not None
    assert identity.agent_id == "agent-a"
    assert identity.method == "jwt"


def test_mtls_and_a_conflicting_secret_are_refused_not_merged(tmp_path: Path) -> None:
    from .identity_support import der_of, issue_client_cert, make_ca

    ca = make_ca(tmp_path, "test-ca")
    crt, _key = issue_client_cert(ca, tmp_path, "some-cn")
    secret = generate_secret()
    table = BindingTable(
        [
            Binding("mtls", "agent-cert", cn="some-cn"),
            Binding("secret", "agent-secret", secret_hash=hash_secret(secret)),
        ]
    )
    resolver = IdentityResolver(bindings=table, mtls_enabled=True, allow_shared_secret=True)

    with pytest.raises(IdentityError) as exc:
        resolver.resolve(peer_cert_der=der_of(crt), identity_token=secret)

    assert (exc.value.status, exc.value.code) == (403, "identity_conflict")


def test_claims_map_the_way_the_in_process_sdk_maps_them() -> None:
    token = make_token(
        KEY, claims={"oid": "user-oid", "tid": "tenant-1", "roles": ["OrderViewer", "Auditor"]}
    )

    identity = _resolver().resolve(peer_cert_der=None, identity_token=token)

    assert identity is not None
    assert identity.identity_claims["oid"] == "user-oid"
    assert identity.identity_roles == ["OrderViewer", "Auditor"]
    assert identity.agent_identity_claims == {"client_id": "app-1"}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("abc.def.ghi", "abc.def.ghi"),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_token_from_header(header: str | None, expected: str | None) -> None:
    assert token_from_header(header) == expected


# ── bindings ─────────────────────────────────────────────────────────────


def test_anonymous_cannot_be_bound() -> None:
    with pytest.raises(BindingError, match="reserved"):
        BindingTable([Binding("jwt", "anonymous", issuer=ISSUER, subject="app-1")])


@pytest.mark.parametrize("bad", ['agent"x', "agent x", "a\\b", "", "-lead", "a" * 200])
def test_an_agent_id_that_could_rewrite_a_cedar_principal_is_rejected(bad: str) -> None:
    with pytest.raises(BindingError):
        BindingTable([Binding("jwt", bad, issuer=ISSUER, subject="app-1")])


def test_one_identity_bound_to_two_agents_is_ambiguous_and_rejected() -> None:
    with pytest.raises(BindingError, match="bound to both"):
        BindingTable(
            [
                Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1"),
                Binding("jwt", "agent-b", issuer=ISSUER, subject="app-1"),
            ]
        )


def test_the_same_binding_twice_is_harmless() -> None:
    table = BindingTable([Binding("mtls", "agent-a", cn="c")] * 2)

    assert table.agent_for_mtls("c") == "agent-a"


def test_the_same_subject_under_a_different_issuer_is_a_different_identity() -> None:
    table = BindingTable([Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1")])

    assert table.agent_for_jwt(ISSUER, "app-1") == "agent-a"
    assert table.agent_for_jwt("https://other-tenant.example/", "app-1") is None


def test_a_secret_binding_looks_up_by_hash_not_by_agent_id() -> None:
    secret_hash = hash_secret("s3cret-value")
    table = BindingTable([Binding("secret", "agent-a", secret_hash=secret_hash)])

    assert table.agent_for_secret(secret_hash) == "agent-a"
    assert table.agent_for_secret(hash_secret("wrong-value")) is None


def test_a_secret_hash_must_look_like_one() -> None:
    # not 64 hex chars -> reject at load time, not silently at lookup time
    with pytest.raises(BindingError, match="not a sha256 hexdigest"):
        BindingTable(
            [Binding("secret", "agent-a", secret_hash="not-a-real-hash")]  # noqa: S106
        )


def test_the_same_secret_hash_bound_to_two_agents_is_ambiguous_and_rejected() -> None:
    secret_hash = hash_secret("shared-by-mistake")
    with pytest.raises(BindingError, match="bound to both"):
        BindingTable(
            [
                Binding("secret", "agent-a", secret_hash=secret_hash),
                Binding("secret", "agent-b", secret_hash=secret_hash),
            ]
        )


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "jwt", "agent_id": "a", "issuer": ISSUER},  # no subject
        {"kind": "jwt", "agent_id": "a", "subject": "s"},  # no issuer
        {"kind": "mtls", "agent_id": "a"},  # no cn
        {"kind": "secret", "agent_id": "a"},  # no secret_hash
        {"kind": "saml", "agent_id": "a"},  # unknown kind
        {"kind": "jwt"},  # no agent_id
    ],
)
def test_incomplete_bindings_are_rejected(record: dict[str, Any]) -> None:
    with pytest.raises(BindingError):
        BindingTable.from_dicts([record])


def test_bindings_load_from_a_list_or_an_object(tmp_path: Path) -> None:
    record = {"kind": "mtls", "cn": "fib-sales", "agent_id": "agent-a"}
    as_list, as_object = tmp_path / "list.json", tmp_path / "obj.json"
    as_list.write_text(json.dumps([record]))
    as_object.write_text(json.dumps({"bindings": [record]}))

    assert BindingTable.from_file(as_list).agent_for_mtls("fib-sales") == "agent-a"
    assert BindingTable.from_file(as_object).agent_for_mtls("fib-sales") == "agent-a"


def test_an_unreadable_bindings_file_stops_startup(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json")

    with pytest.raises(BindingError, match="cannot read"):
        BindingTable.from_file(tmp_path / "bad.json")
    with pytest.raises(BindingError, match="cannot read"):
        BindingTable.from_file(tmp_path / "missing.json")


# ── JWKS key sourcing ────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _jwks(*kids: str) -> dict[str, Any]:
    return {"keys": [jwk_for(KEY, kid) for kid in kids]}


def test_a_plain_http_jwks_url_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        JwksKeyProvider("http://idp.example/keys")


@respx.mock
def test_keys_are_fetched_once_and_cached() -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks("k1")))
    provider = JwksKeyProvider(JWKS_URL)

    assert provider.get_key("k1") is not None
    assert provider.get_key("k1") is not None

    assert route.call_count == 1


@respx.mock
def test_a_key_that_appears_after_rotation_is_picked_up_once_the_rate_limit_allows() -> None:
    clock = _Clock()
    respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=_jwks("old")),
            httpx.Response(200, json=_jwks("old", "new")),
        ]
    )
    provider = JwksKeyProvider(JWKS_URL, min_refresh_s=30, clock=clock)
    assert provider.get_key("old") is not None

    assert provider.get_key("new") is None  # too soon to refetch
    clock.now += 31
    assert provider.get_key("new") is not None


@respx.mock
def test_unknown_kids_cannot_be_used_to_hammer_the_idp() -> None:
    clock = _Clock()
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks("k1")))
    provider = JwksKeyProvider(JWKS_URL, min_refresh_s=30, clock=clock)

    for i in range(200):
        assert provider.get_key(f"random-{i}") is None

    assert route.call_count == 1


@respx.mock
def test_an_unreachable_idp_with_nothing_cached_trusts_nothing() -> None:
    respx.get(JWKS_URL).mock(side_effect=httpx.ConnectError("down"))

    assert JwksKeyProvider(JWKS_URL).get_key("k1") is None


@respx.mock
def test_an_outage_keeps_verifying_with_known_keys_only_for_a_bounded_time() -> None:
    clock = _Clock()
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks("k1")))
    provider = JwksKeyProvider(JWKS_URL, ttl_s=100, min_refresh_s=1, max_stale_s=1000, clock=clock)
    assert provider.get_key("k1") is not None

    route.side_effect = httpx.ConnectError("down")  # the IdP goes away for good
    clock.now += 200  # past the TTL: a refetch is attempted, fails, known key still served
    assert provider.get_key("k1") is not None
    assert provider.get_key("never-seen") is None  # an outage never mints trust

    clock.now += 2000  # down for longer than we will trust the cache
    assert provider.get_key("k1") is None


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"no": "keys"}),
        httpx.Response(200, json={"keys": "not a list"}),
    ],
)
def test_a_bad_jwks_response_is_a_failed_fetch_not_a_crash(response: httpx.Response) -> None:
    respx.get(JWKS_URL).mock(return_value=response)

    assert JwksKeyProvider(JWKS_URL).get_key("k1") is None


@respx.mock
def test_one_malformed_key_does_not_discard_the_rest_and_encryption_keys_are_skipped() -> None:
    document = {
        "keys": [
            {"kid": "broken", "kty": "RSA", "n": "!!!", "e": "AQAB"},
            {"kid": "no-type"},
            "not-an-object",
            jwk_for(KEY, "enc-only", use="enc"),
            jwk_for(KEY, "good"),
        ]
    }
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=document))
    provider = JwksKeyProvider(JWKS_URL, min_refresh_s=0)

    assert provider.get_key("good") is not None
    assert provider.get_key("broken") is None
    assert provider.get_key("enc-only") is None


# ── startup validation (half-configured must not look secured) ───────────


def _settings(**overrides: Any) -> Settings:
    base = {
        "idp_issuer": None,
        "idp_jwks_url": None,
        "idp_audiences": (),
        "tls_client_ca": None,
        "identity_bindings_path": None,
        "require_verified_identity": False,
    }
    return dataclasses.replace(Settings(), **{**base, **overrides})


def test_nothing_configured_means_no_resolver_and_the_path_claim_applies() -> None:
    assert build_resolver(settings=_settings()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"idp_issuer": ISSUER},
        {"idp_issuer": ISSUER, "idp_jwks_url": JWKS_URL},
        {"idp_jwks_url": JWKS_URL, "idp_audiences": (AUDIENCE,)},
        {"idp_audiences": (AUDIENCE,)},
    ],
)
def test_a_partly_configured_idp_stops_startup(overrides: dict[str, Any]) -> None:
    with pytest.raises(RuntimeError, match="partly configured"):
        build_resolver(settings=_settings(**overrides))


def test_requiring_verified_identity_with_no_method_stops_startup() -> None:
    with pytest.raises(RuntimeError, match="no identity method"):
        build_resolver(settings=_settings(require_verified_identity=True))


def test_an_identity_method_with_no_bindings_stops_startup() -> None:
    settings = _settings(idp_issuer=ISSUER, idp_jwks_url=JWKS_URL, idp_audiences=(AUDIENCE,))

    with pytest.raises(RuntimeError, match="IDENTITY_BINDINGS"):
        build_resolver(settings=settings)


def test_shared_secret_alone_is_a_sufficient_identity_method(tmp_path: Path) -> None:
    secret = generate_secret()
    bindings = tmp_path / "b.json"
    bindings.write_text(
        json.dumps([{"kind": "secret", "agent_id": "agent-a", "secret_hash": hash_secret(secret)}])
    )
    settings = _settings(
        allow_shared_secret=True,
        identity_bindings_path=str(bindings),
        require_verified_identity=True,
    )

    resolver = build_resolver(settings=settings)

    assert resolver is not None
    identity = resolver.resolve(peer_cert_der=None, identity_token=secret)
    assert identity is not None and identity.agent_id == "agent-a"


def test_allow_shared_secret_alongside_required_mtls_warns_but_still_starts(
    tmp_path: Path,
) -> None:
    # Not a startup error (a gateway may legitimately be mid-migration), but
    # this combination can never admit a shared-secret-only caller: the TLS
    # handshake with tls_client_auth=required refuses them before any
    # request-level code runs. Documented in config.py/resolver.py; proven
    # here so a future change can't silently make this combination fail
    # closed in a way an operator wouldn't notice.
    bindings = tmp_path / "b.json"
    bindings.write_text(json.dumps([{"kind": "mtls", "agent_id": "agent-a", "cn": "c"}]))
    settings = _settings(
        tls_client_ca="/dev/null",
        tls_client_auth="required",
        allow_shared_secret=True,
        identity_bindings_path=str(bindings),
    )

    resolver = build_resolver(settings=settings)

    assert resolver is not None


def test_a_complete_config_builds_a_resolver(tmp_path: Path) -> None:
    bindings = tmp_path / "b.json"
    bindings.write_text(
        json.dumps([{"kind": "jwt", "issuer": ISSUER, "subject": "app-1", "agent_id": "agent-a"}])
    )
    settings = _settings(
        idp_issuer=ISSUER,
        idp_jwks_url=JWKS_URL,
        idp_audiences=(AUDIENCE,),
        identity_bindings_path=str(bindings),
        require_verified_identity=True,
    )

    resolver = build_resolver(settings=settings)

    assert resolver is not None
    assert resolver.require_verified is True
