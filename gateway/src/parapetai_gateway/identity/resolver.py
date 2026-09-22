"""Resolve a request's credentials into ONE verified agent identity, or refuse.

Three credential methods:

1. mTLS    -- the client certificate's CN (validated by the TLS layer).
2. JWT     -- an IdP-issued token in the identity header, verified against
              the IdP's published keys.
3. secret  -- a per-agent bearer secret in the SAME identity header (never
              `authorization` -- see config.py's allow_shared_secret), for a
              caller with no client certificate and no IdP token. Disabled by
              default; mTLS is the one enabled by default and stays that way
              regardless of whether secret is also turned on. A single header
              value can only ever be tried as ONE of JWT/secret -- whichever
              its shape matches (see _looks_like_jwt) -- never both, so a
              well-formed JWT is never reinterpreted as a bearer secret or
              vice versa.

Rules that make this safe rather than merely convenient:

* A credential that is PRESENTED but does not verify is refused (401). It never
  falls through to a weaker method or to the unauthenticated path claim -- an
  attacker must not be able to downgrade by sending garbage.
* A credential that verifies but has no binding is refused (403). Verification
  says who the IdP vouches for; only a binding says which agent that is.
* Two valid credentials that resolve to different agents are refused (403),
  not merged.
* No credential at all is not an error here: the caller decides (the gateway
  falls back to the path claim unless PARAPETAI_REQUIRE_VERIFIED_IDENTITY).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jwt
import structlog

from parapetai_agent.agent_secrets import hash_secret
from parapetai_agent.token_identity import agent_identity_from_claims, identity_from_claims
from parapetai_gateway.identity.bindings import BindingTable
from parapetai_gateway.identity.jwks import KeyProvider
from parapetai_gateway.identity.mtls import common_name

log = structlog.get_logger(__name__)


def _looks_like_jwt(token: str) -> bool:
    """A JWT is always header.payload.signature -- exactly two dots. A
    generated bearer secret (secrets.token_urlsafe(32)) never contains one,
    so this cheaply and reliably tells the two credential shapes apart
    without attempting to parse either."""
    return token.count(".") == 2

# Symmetric and "none" algorithms are never acceptable here: verification is
# against the IdP's PUBLIC keys, and allowing HS* is the classic
# algorithm-confusion attack (public key used as an HMAC secret).
_FORBIDDEN_ALGORITHM_PREFIXES = ("HS", "NONE")


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    agent_id: str  # from the binding, never from the token or the URL
    method: str  # "mtls" | "jwt" | "mtls+jwt"
    subject: str  # the CN or token subject that was bound
    issuer: str | None = None
    identity_claims: dict[str, str] = field(default_factory=dict)
    identity_roles: list[str] = field(default_factory=list)
    agent_identity_claims: dict[str, str] = field(default_factory=dict)


class IdentityError(Exception):
    """A credential was presented and refused. `code` is stable and safe to
    show a client; the underlying reason goes to the log only, so a caller
    cannot use the response to learn why a forged token failed."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class IdpConfig:
    issuer: str
    audiences: tuple[str, ...]
    algorithms: tuple[str, ...] = ("RS256",)
    agent_claims: tuple[str, ...] = ("azp", "appid", "client_id")
    leeway_s: int = 30

    def __post_init__(self) -> None:
        if not self.issuer or not self.audiences:
            raise ValueError("an IdP needs an issuer and at least one audience")
        if not self.algorithms:
            raise ValueError("an IdP needs at least one accepted signing algorithm")
        for alg in self.algorithms:
            if alg.upper().startswith(_FORBIDDEN_ALGORITHM_PREFIXES):
                raise ValueError(f"signing algorithm {alg!r} is not allowed")


class JwtVerifier:
    def __init__(self, config: IdpConfig, keys: KeyProvider) -> None:
        self._config = config
        self._keys = keys

    def verify(self, token: str) -> dict[str, Any]:
        """Verified claims, or IdentityError(401). Signature, `iss`, `aud`,
        `exp` and the algorithm allowlist are all mandatory."""
        try:
            header = jwt.get_unverified_header(token)
            alg = header.get("alg")
            kid = header.get("kid")
            if alg not in self._config.algorithms:
                raise jwt.InvalidAlgorithmError(f"algorithm {alg!r} not accepted")
            if not isinstance(kid, str) or not kid:
                raise jwt.InvalidTokenError("token has no kid")
            key = self._keys.get_key(kid)
            if key is None:
                raise jwt.InvalidTokenError(f"no trusted key for kid {kid!r}")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=list(self._config.audiences),
                issuer=self._config.issuer,
                leeway=self._config.leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            log.warning("identity_token_rejected", reason=type(exc).__name__, detail=str(exc))
            raise IdentityError(401, "invalid_token") from exc
        return claims

    def subject_of(self, claims: Mapping[str, Any]) -> str | None:
        """The caller's agent identity: the first configured claim present."""
        for name in self._config.agent_claims:
            value = claims.get(name)
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def issuer(self) -> str:
        return self._config.issuer


class IdentityResolver:
    def __init__(
        self,
        *,
        bindings: BindingTable,
        jwt_verifier: JwtVerifier | None = None,
        mtls_enabled: bool = False,
        require_verified: bool = False,
        allow_shared_secret: bool = False,
    ) -> None:
        if jwt_verifier is None and not mtls_enabled and not allow_shared_secret:
            raise ValueError("an IdentityResolver needs at least one identity method")
        # Reassigned wholesale to change bindings (BindingTable is immutable).
        self.bindings = bindings
        self._jwt = jwt_verifier
        self._mtls = mtls_enabled
        self._allow_secret = allow_shared_secret
        self.require_verified = require_verified

    @property
    def mtls_enabled(self) -> bool:
        return self._mtls

    @property
    def jwt_enabled(self) -> bool:
        return self._jwt is not None

    @property
    def shared_secret_enabled(self) -> bool:
        return self._allow_secret

    def resolve(
        self, *, peer_cert_der: bytes | None, identity_token: str | None
    ) -> VerifiedIdentity | None:
        """None means "no credential presented". Anything else is a verified
        identity or an IdentityError -- there is no third outcome."""
        from_cert = self._from_cert(peer_cert_der) if self._mtls and peer_cert_der else None
        header_configured = self._jwt is not None or self._allow_secret
        from_header = (
            self._from_header(identity_token)
            if identity_token and header_configured
            else None
        )

        if from_cert and from_header:
            if from_cert.agent_id != from_header.agent_id:
                log.warning(
                    "identity_conflict",
                    mtls_agent=from_cert.agent_id,
                    header_agent=from_header.agent_id,
                )
                raise IdentityError(403, "identity_conflict")
            return VerifiedIdentity(
                agent_id=from_header.agent_id,
                method=f"mtls+{from_header.method}",
                subject=from_header.subject,
                issuer=from_header.issuer,
                identity_claims=from_header.identity_claims,
                identity_roles=from_header.identity_roles,
                agent_identity_claims=from_header.agent_identity_claims,
            )
        return from_cert or from_header

    def _from_header(self, token: str) -> VerifiedIdentity:
        """Dispatches on the credential's SHAPE, not gateway configuration
        alone: a JWT-shaped value is always tried as a JWT when JWT identity
        is enabled, even if shared-secret is also enabled -- a well-formed
        JWT must never be looked up as if it were an opaque secret."""
        if self._jwt is not None and (_looks_like_jwt(token) or not self._allow_secret):
            return self._from_token(token)
        return self._from_secret(token)

    def _from_cert(self, der: bytes) -> VerifiedIdentity:
        cn = common_name(der)
        if cn is None:
            log.warning("identity_cert_rejected", reason="no single subject CN")
            raise IdentityError(401, "invalid_client_certificate")
        agent_id = self.bindings.agent_for_mtls(cn)
        if agent_id is None:
            log.warning("identity_unbound", method="mtls", subject=cn)
            raise IdentityError(403, "identity_not_bound")
        return VerifiedIdentity(agent_id=agent_id, method="mtls", subject=cn)

    def _from_secret(self, token: str) -> VerifiedIdentity:
        # Unlike mTLS/JWT there is no separate "verify, then look up the
        # binding" step: the secret's hash IS the lookup key, so a wrong or
        # garbage secret and an unbound one are indistinguishable -- both are
        # a credential that was presented but did not verify (401), not
        # identity_not_bound (403), which would wrongly imply the gateway
        # recognized who was calling.
        agent_id = self.bindings.agent_for_secret(hash_secret(token))
        if agent_id is None:
            log.warning("identity_secret_rejected", reason="unknown or incorrect secret")
            raise IdentityError(401, "invalid_secret")
        return VerifiedIdentity(agent_id=agent_id, method="secret", subject=agent_id)

    def _from_token(self, token: str) -> VerifiedIdentity:
        assert self._jwt is not None
        claims = self._jwt.verify(token)
        subject = self._jwt.subject_of(claims)
        if subject is None:
            log.warning("identity_token_rejected", reason="no agent claim present")
            raise IdentityError(401, "invalid_token")
        agent_id = self.bindings.agent_for_jwt(self._jwt.issuer, subject)
        if agent_id is None:
            log.warning("identity_unbound", method="jwt", subject=subject)
            raise IdentityError(403, "identity_not_bound")
        # Same claim mapping the in-process SDK applies to the same token, so
        # a role-gated Cedar policy sees identical context either way.
        end_user_claims, roles = identity_from_claims(claims)
        agent = agent_identity_from_claims(claims)
        return VerifiedIdentity(
            agent_id=agent_id,
            method="jwt",
            subject=subject,
            issuer=self._jwt.issuer,
            identity_claims=end_user_claims,
            identity_roles=roles,
            agent_identity_claims=agent[0] if agent else {},
        )


def token_from_header(value: str | None) -> str | None:
    """`Bearer <jwt>` or a bare token; None when absent or empty."""
    if not value:
        return None
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() == "bearer":
        return rest.strip() or None  # "Bearer" with nothing after it is no token
    return value.strip() or None


def build_resolver(
    *,
    settings: Any,
    keys: KeyProvider | None = None,
    bindings: BindingTable | None = None,
) -> IdentityResolver | None:
    """The resolver the settings describe, or None when no identity method is
    configured (the gateway then trusts the path claim, as before).

    Half-configured is an error, not a fallback: an IdP with no audience, or
    PARAPETAI_REQUIRE_VERIFIED_IDENTITY with no method to verify anything,
    would otherwise start up looking secured while being open or unusable.
    """
    from parapetai_gateway.identity.jwks import JwksKeyProvider

    jwt_verifier: JwtVerifier | None = None
    if settings.jwt_identity_enabled:
        missing = [
            name
            for name, present in (
                ("PARAPETAI_IDP_ISSUER", settings.idp_issuer),
                ("PARAPETAI_IDP_JWKS_URL", settings.idp_jwks_url or keys),
                ("PARAPETAI_IDP_AUDIENCE", settings.idp_audiences),
            )
            if not present
        ]
        if missing:
            raise RuntimeError(f"JWT identity is partly configured; also set {', '.join(missing)}")
        jwt_verifier = JwtVerifier(
            IdpConfig(
                issuer=settings.idp_issuer,
                audiences=tuple(settings.idp_audiences),
                algorithms=tuple(settings.idp_algorithms),
                agent_claims=tuple(settings.idp_agent_claims),
            ),
            keys or JwksKeyProvider(settings.idp_jwks_url),
        )

    shared_secret_unreachable = (
        settings.allow_shared_secret
        and settings.mtls_enabled
        and settings.tls_client_auth == "required"
    )
    if shared_secret_unreachable:
        # Not a startup error, but very likely a mistake: PARAPETAI_TLS_CLIENT_AUTH=
        # required refuses the TLS handshake for a caller with no client
        # certificate before any request-level code (including this
        # resolver) ever runs, so a shared-secret-only caller can never
        # reach it. Warn loudly rather than silently accept a flag that can
        # never take effect -- see docs/reference/gateway-identity.md.
        log.warning(
            "shared_secret_unreachable",
            reason=(
                "PARAPETAI_ALLOW_SHARED_SECRET is set but PARAPETAI_TLS_CLIENT_AUTH=required "
                "refuses every connection with no client certificate at the TLS handshake, "
                "before a shared secret could ever be checked; set "
                "PARAPETAI_TLS_CLIENT_AUTH=optional"
            ),
        )

    if jwt_verifier is None and not settings.mtls_enabled and not settings.allow_shared_secret:
        if settings.require_verified_identity:
            raise RuntimeError(
                "PARAPETAI_REQUIRE_VERIFIED_IDENTITY is set but no identity method is "
                "configured (set PARAPETAI_IDP_*, PARAPETAI_TLS_CLIENT_CA and/or "
                "PARAPETAI_ALLOW_SHARED_SECRET); every request would be refused"
            )
        return None

    if bindings is None:
        if not settings.identity_bindings_path:
            raise RuntimeError(
                "an identity method is configured but PARAPETAI_IDENTITY_BINDINGS is not: "
                "with no bindings every verified caller would be refused"
            )
        bindings = BindingTable.from_file(settings.identity_bindings_path)

    return IdentityResolver(
        bindings=bindings,
        jwt_verifier=jwt_verifier,
        mtls_enabled=settings.mtls_enabled,
        require_verified=settings.require_verified_identity,
        allow_shared_secret=settings.allow_shared_secret,
    )


__all__: Sequence[str] = [
    "IdentityError",
    "IdentityResolver",
    "IdpConfig",
    "JwtVerifier",
    "VerifiedIdentity",
    "build_resolver",
    "token_from_header",
]
