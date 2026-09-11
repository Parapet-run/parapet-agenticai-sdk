"""Token-based identity extraction for parapetai_agent.maf.

Answers a real gap: nothing in Microsoft Agent Framework decodes a bearer
token into identity for you (verified directly against the installed
package -- no Identity/Credential/Principal/Claims class anywhere;
AzureCredentialTypes authenticates the AGENT to a provider, it does not
carry or decode an end user's claims -- see
docs/maf-in-process-integration.md for the full investigation). This module
is what decodes one, for MCP's bearer-token connections specifically
(MCPStreamableHTTPTool/SecureMCPToolProxy accept
`headers={"Authorization": "Bearer <token>"}`) and anywhere else a raw
token needs turning into parapetai_agent.maf's identity_claims/identity_roles
shape.

## Is an MCP bearer token always a JWT?

No -- verified directly against MCP's own Authorization specification
(modelcontextprotocol.io/specification/.../basic/authorization), not
assumed. MCP's auth is OAuth 2.1: it requires `Authorization: Bearer
<token>` and OAuth 2.1 resource-server-side validation, but does not
mandate a token format. An opaque token (validated via introspection
against the Authorization Server, RFC 7662) is equally spec-compliant --
this module cannot decode one locally, and does not pretend to
(decode_jwt_claims returns None, with a logged warning, for anything that
isn't structurally a JWT). In practice most real IdPs -- Entra ID among
them -- issue JWT access tokens, so JWT is the common case this module
optimises for, but "common" is not "guaranteed by the spec."

## Where "on-behalf-of" actually has a standard place

Three real, verified standards, checked in this order by
agent_identity_from_claims():

1. RFC 8693 (OAuth 2.0 Token Exchange)'s `act` (actor) claim -- the formal
   standard for representing a delegation chain in a JWT. `act` is a
   nested JSON object; `act.sub` identifies the current actor (the
   agent/service acting on the subject's behalf). A further-nested
   `act.act` would represent a prior actor in a longer chain, but per the
   RFC, only the OUTERMOST act matters for access-control decisions --
   nested ones are informational. Confirmed directly from RFC 8693, not
   assumed.
2. `client_id` -- RFC 9068 (JWT Profile for OAuth 2.0 Access Tokens) §2.2's
   own REQUIRED claim (itself defined in RFC 8693 §4.3): "identifies the
   client that requested the token." Checked directly against the RFC
   text, not assumed -- a real, previously-shipped gap: an RFC-9068-
   compliant access token (the standard shape for a JWT-formatted OAuth
   access token, distinct from an OIDC ID token -- flagged via a
   `typ: "at+jwt"` JOSE header per §2.1) is REQUIRED to carry this claim,
   yet nothing here ever read it -- only `act`/`azp`/`appid` were checked,
   so a spec-compliant access token's agent identity was silently missed
   unless one of THOSE happened to be redundantly present too.
3. `azp` (OIDC Core's "Authorized Party" claim) or `appid` (the same
   concept in Entra ID v1.0 tokens, which predate azp) -- the client ID
   the token was issued to. Common in practice even without a full RFC
   8693 token-exchange round trip: an Entra delegated-permission access
   token typically carries the calling app's client ID here alongside the
   user's own oid/sub.

## Canonical claim schema -- one shape, normalized across every source

Four real token/response shapes reach this module: a JWT ID token or
RFC 9068 JWT access token (decoded locally by decode_jwt_claims()), an
RFC 7662 introspection response, and an OIDC UserInfo response (both of
the latter two arriving via BackgroundOpaqueTokenResolver, for an opaque
token neither RFC 6749 nor RFC 6750 gives this module any local way to
decode). Each uses slightly different field names for the same concepts.
Every one of them is normalized onto ONE canonical vocabulary --
`_END_USER_CLAIM_KEYS`/`_AGENT_CLAIM_KEYS` below -- before
identity_from_claims()/agent_identity_from_claims() ever see it, so
those two functions are written against exactly one schema regardless of
which source produced it. `_normalize_introspection_claims()` is the
ONE place a source-specific rename happens (currently: RFC 7662's
`username` -> `preferred_username`); every other field already arrives
under its canonical name (an OIDC UserInfo response uses JWT-identical
names throughout; RFC 7662's `client_id`/`scope`/`sub`/`aud`/`iss`/
`exp`/`iat`/`nbf`/`jti` already match RFC 9068's own names for the same
fields). `_claims_subset()` then reads only the canonical keys listed
below -- an unrecognized field from any source is simply not copied,
never guessed at.

- `sub` -- RFC 7519 §4.1.2 (registered). Subject: resource owner (3LO) or
  client (2LO) per RFC 9068 §2.2.
- `iss` -- RFC 7519 §4.1.1 (registered). Issuer.
- `aud` -- RFC 7519 §4.1.3 (registered). Audience; may be a JSON array
  (comma-joined here, see _claim_to_str()).
- `exp` / `iat` / `nbf` -- RFC 7519 §4.1.4/.6/.5 (registered). Token
  lifetime, as decimal-string epoch seconds.
- `jti` -- RFC 7519 §4.1.7 (registered). Unique token id.
- `client_id` -- RFC 9068 §2.2 (REQUIRED) / RFC 8693 §4.3 / RFC 7662 §2.2.
  Which OAuth client this token was issued to -- the RFC-standard name;
  `azp`/`appid` below are the same concept under a vendor/OIDC name.
- `oid` / `tid` -- Entra ID-specific, no RFC. Object id / tenant id --
  Microsoft's own claim names, not from any IETF/OpenID spec.
- `preferred_username` / `upn` / `email` -- OIDC Core §5.1 standard
  claims (`upn` is Entra-specific). Human-readable identifiers, in
  priority order for display.
- `name` / `given_name` / `family_name` -- OIDC Core §5.1 standard claims.
- `azp` -- OIDC Core §2 ("Authorized Party"). Same concept as
  `client_id`; `appid` is the Entra v1.0 predecessor of this claim.
- `amr` -- OIDC Core §2 / RFC 9068 §2.2.1 (optional). Authentication
  Methods References.
- `scp` / `scope` -- `scp` is Entra-specific; `scope` is RFC 9068
  §2.2.3 / RFC 6749 §3.3 / RFC 7662 §2.2. Space-delimited OAuth scopes;
  both extracted since either name may appear.
- `entitlements` -- RFC 9068 §2.2.3.1 (optional). A distinct
  authorization-attribute type, not a roles/groups synonym.
- `roles` / `groups` -- Entra-specific / RFC 9068 §2.2.3.1 (optional).
  A SET, not a scalar -- kept in identity_roles, not identity_claims
  (see Snapshot.identity_roles's own docstring).

`act` (RFC 8693 §4.1) is handled separately, not folded into this list:
it is a NESTED object, one level of indirection representing a distinct
identity (the agent/actor), not a flat claim on the outer token -- see
agent_identity_from_claims()'s own docstring for its three-tier
resolution order (act, then client_id, then azp/appid).

## Signature verification -- what this module does and does not do

Claims-only decode (the JWT payload segment, base64url-decoded), like
examples/maf_webapp/entra_login.py's use of MSAL's
id_token_claims -- NOT full JWKS signature verification. That was
justified there because the caller (that script) IS the client that just
talked to the IdP's token endpoint directly over TLS -- per OpenID Connect
Core sec 3.1.3.7 step 6, signature verification is optional in that exact
acquisition path, TLS is the trust anchor instead. The same justification
applies here ONLY if this process already legitimately possesses the
token because it (or something it trusts, e.g. its own OBO exchange)
fetched it -- i.e. this module is for the case where parapetai_agent.maf is
acting as the calling AGENT (the OAuth/MCP *client*), holding a token it
is about to present outbound. It is explicitly NOT sufficient, on its own,
for a process acting as an MCP *server* validating an inbound bearer token
from an untrusted remote caller -- MCP's own spec requires real audience
validation and signature verification for that role (see the
"Access Token Privilege Restriction" section of the spec above), which
this module does not implement and does not claim to.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ExtractedIdentity:
    """What a TokenIdentityExtractor returns -- both halves optional by
    design (parapetai_agent.maf._Identity's own docstring explains why end
    user identity is optional; agent identity is optional for the same
    reason plus one more: most tokens simply don't carry a delegation
    claim at all, which is a normal case, not an error)."""

    end_user_claims: dict[str, str] = field(default_factory=dict)
    end_user_roles: list[str] = field(default_factory=list)
    agent_claims: dict[str, str] = field(default_factory=dict)
    agent_roles: list[str] = field(default_factory=list)


class TokenIdentityExtractor(Protocol):
    """Pluggable extraction point -- implement this for a token
    format/IdP other than OIDC/JWT (an opaque token needing introspection,
    a different framework's own token shape, ...) and pass it to
    identity_from_bearer_token(extractor=...) instead of the default
    JwtIdentityExtractor. Deliberately a Protocol, not a base class to
    subclass -- any object with a matching extract() method works,
    including one defined by a future framework this repo doesn't
    otherwise depend on."""

    def extract(self, token: str) -> ExtractedIdentity: ...


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    """Claims-only decode of a JWT's payload segment. See the module
    docstring's "Signature verification" section for exactly what this
    does and doesn't prove, and when that's an appropriate tradeoff.

    Returns None (never raises, always logs why) if `token` isn't
    structurally a JWT -- three dot-separated segments, a base64url JSON
    payload -- rather than guessing or silently returning {}. The caller
    ends up with empty identity_claims/identity_roles either way, and any
    Cedar policy that requires identity fails closed on that (context has
    identity_roles is false) -- correct, since "identity present but
    undecodable" and "identity genuinely absent" should both deny a
    role-gated action, not open one. The logged warning is what lets an
    operator tell those two cases apart after the fact.
    """
    parts = token.split(".")
    if len(parts) != 3:
        log.warning("token_not_jwt: expected 3 dot-separated segments, got %d", len(parts))
        return None
    try:
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        log.warning("token_jwt_decode_failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        log.warning("token_jwt_decode_failed: payload is not a JSON object")
        return None
    return payload


# Standard/Entra claim names for the END USER, in three groups:
#
# 1. Identity/display (unchanged from before this list grew): oid/tid
#    (Entra), sub (OIDC standard subject), preferred_username/upn (Entra
#    display name variants), email.
# 2. RFC 7519 §4.1 registered JWT claims that carry real operational
#    meaning for an audit record, not just identity -- `iss` (issuer: WHICH
#    IdP vouched for this), `aud` (audience: which resource this token was
#    minted for -- see the `resource` note below for why this, not a
#    fabricated `resource` claim, is the field that actually matters
#    here), `exp`/`iat`/`nbf` (token lifetime -- lets an operator later ask
#    "was this decision made against a token that was, in fact, still
#    valid"), `jti` (JWT ID -- a stable per-token identifier, useful for
#    correlating repeated calls under the SAME token without needing the
#    raw token itself).
# 3. OIDC Core §5.1 standard profile/session claims with a clear, stable
#    meaning across the tokens this repo has actually verified (see
#    docs/maf-in-process-integration.md): `name`/`given_name`/`family_name`
#    (the human's actual name, distinct from a username), `azp` (OIDC's own
#    "authorized party" -- also checked separately by
#    agent_identity_from_claims() as an act-claim fallback, but worth
#    surfacing on the end-user side too since it identifies which client
#    app requested this token even when no delegation chain applies),
#    `amr` (Authentication Methods References -- HOW the user
#    authenticated, e.g. ["pwd","mfa"] -- content-free operational
#    metadata, not a secret), `scp`/`scope` (OAuth2 scopes granted to this
#    token -- `scp` is Entra's naming, `scope` is RFC 6749 §3.3's own
#    space-delimited string; both extracted so whichever the issuing IdP
#    uses is captured, not just one).
#
# `resource` is deliberately NOT here, and is NOT extracted from claims by
# this module at all -- verified directly against RFC 8707 (Resource
# Indicators for OAuth 2.0) and RFC 9728 (OAuth 2.0 Protected Resource
# Metadata, which MCP's own spec aligns to per SEP-985): `resource` is a
# parameter of the AUTHORIZATION/TOKEN REQUEST the CLIENT sends, and the
# protected resource's own canonical identifier (published at
# `/.well-known/oauth-protected-resource`), not a claim RFC 7519 defines or
# a claim any spec guarantees an Authorization Server echoes back into the
# issued token. The resource server's actual RFC 9728 obligation is to
# validate the token's `aud` MATCHES its own resource identifier -- which
# is exactly why `aud` above, not a fabricated `resource` claim lookup, is
# the correct field to surface here. A caller that already knows which
# resource (MCP server URL) a call was made against -- observation.py's
# own target/destination fields on an MCP tool-call observation -- has the
# real value locally; decoding it out of the token is not a thing RFC 8707
# makes possible in general.
_END_USER_CLAIM_KEYS = (
    "oid",
    "sub",
    "tid",
    "preferred_username",
    "upn",
    "email",
    "name",
    "given_name",
    "family_name",
    "iss",
    "aud",
    "exp",
    "iat",
    "nbf",
    "jti",
    "azp",
    "client_id",
    "amr",
    "scp",
    "scope",
    # RFC 9068 §2.2.3.1: OPTIONAL authorization-attribute claims alongside
    # scope -- roles/groups (already captured, see identity_from_claims()'s
    # own roles-then-groups fallback below) plus entitlements, which
    # neither of those covers (a distinct authorization-attribute type per
    # the RFC, not a role/group synonym).
    "entitlements",
)

# Claim names checked, in order, for the AGENT/actor's own identity within
# act -- oid/sub/tid because Entra's act claim (when present) shapes itself
# the same way as the outer token's own claims. `iss` added: RFC 8693 §4.1
# itself gives `iss` as a typical `act` member alongside `sub` ("other
# claims from the security token issued to the actor, such as iss"), so an
# agent identity resolved from a real RFC 8693 token-exchange chain often
# carries its OWN issuer, distinct from the outer token's `iss` above --
# worth keeping separate, not collapsed into one field, since a
# cross-tenant delegation (agent issued by a DIFFERENT IdP than the end
# user) is exactly the case an operator needs `iss` to tell apart.
_AGENT_CLAIM_KEYS = ("oid", "sub", "tid", "iss")


def _claim_to_str(value: Any) -> str:
    """identity_claims is documented (Snapshot.identity_claims's own
    docstring) as a flat dict of SCALAR attributes -- but RFC 7519 §4.1.3
    lets `aud` be either a single string OR a JSON array of strings (a
    token issued for more than one audience). A bare `str(value)` on a
    list claim would silently produce Python's list repr
    (`"['a', 'b']"`) instead of a usable value, so array-valued claims are
    comma-joined here into one real string instead."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _claims_subset(claims: Mapping[str, Any], keys: Sequence[str]) -> dict[str, str]:
    return {k: _claim_to_str(claims[k]) for k in keys if claims.get(k) is not None}


def identity_from_claims(claims: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """End user identity_claims/identity_roles from standard OIDC/Entra
    claim names. Same mapping
    examples/maf_webapp/entra_login.py's claims_to_identity()
    uses for a freshly-acquired MSAL ID token -- that function now calls
    this one, rather than duplicating the mapping."""
    end_user_claims = _claims_subset(claims, _END_USER_CLAIM_KEYS)
    roles = claims.get("roles")
    if not isinstance(roles, list):
        groups = claims.get("groups")
        roles = groups if isinstance(groups, list) else []
    return end_user_claims, [str(r) for r in roles]


def agent_identity_from_claims(
    claims: Mapping[str, Any],
) -> tuple[dict[str, str], list[str]] | None:
    """The delegated-to AGENT's own identity, distinct from the end user
    identity_from_claims() extracts from the SAME claims dict -- see the
    module docstring's "Where on-behalf-of actually has a standard place"
    section for exactly which THREE standards this checks and why, in
    order: RFC 8693's `act` claim, then RFC 9068's `client_id`, then
    azp/appid.

    Returns None -- not an empty-but-present ExtractedIdentity -- when
    none of these is in `claims`. That's the common case for a plain
    end-user token with no delegation involved, not an error condition."""
    act = claims.get("act")
    if isinstance(act, dict) and act:
        return _claims_subset(act, _AGENT_CLAIM_KEYS), []
    # RFC 9068 §2.2's REQUIRED `client_id` claim (defined in RFC 8693
    # §4.3) -- checked before azp/appid: it's the RFC-standard name for
    # the identical concept those two represent as OIDC/Entra-specific
    # conventions, so a token carrying the standard name should resolve
    # the same way one carrying a vendor-specific name already does.
    for key in ("client_id", "azp", "appid"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return {"client_id": value}, []
    return None


def _extracted_identity_from_claims(claims: Mapping[str, Any]) -> ExtractedIdentity:
    """Shared by JwtIdentityExtractor (a locally-decoded JWT payload) and
    the opaque-token path below (an RFC 7662 introspection response or an
    OIDC UserInfo response, both just claims dicts by the time they reach
    here) -- one place turns a claims mapping into BOTH end-user and agent
    identity, so the two sources can never drift into different
    extraction logic."""
    end_user_claims, end_user_roles = identity_from_claims(claims)
    agent = agent_identity_from_claims(claims)
    agent_claims, agent_roles = agent if agent is not None else ({}, [])
    return ExtractedIdentity(
        end_user_claims=end_user_claims,
        end_user_roles=end_user_roles,
        agent_claims=agent_claims,
        agent_roles=agent_roles,
    )


# ---------------------------------------------------------------------------
# Opaque (non-JWT) token resolution -- RFC 7662 (OAuth 2.0 Token
# Introspection) and OIDC Core §5.3 (UserInfo Endpoint), resolved OFF the
# request path.
#
# The module docstring's "Is an MCP bearer token always a JWT?" section
# already establishes that an opaque token is equally spec-compliant and
# that this module cannot decode one locally. The naive fix -- have
# extract() call the Authorization Server synchronously when it sees a
# non-JWT token -- would add a real network round trip's latency to every
# governed call presenting an opaque token, on the hot path Cedar's own
# decision already sits on. That is the exact mistake control_plane.py's
# own bundle sync avoids: a decision there is evaluated against whatever
# bundle is ALREADY LOADED locally, refreshed by a background poller
# (run_bundle_poller) that never blocks a request waiting on it.
# BackgroundOpaqueTokenResolver applies the identical shape here: a
# request-path lookup (get_cached) is a synchronous in-memory dict read,
# ALWAYS fast; the actual introspection/userinfo HTTP call happens on one
# daemon worker thread, off any request's path entirely.
#
# What this means for the FIRST call ever seen with a given opaque token:
# it resolves with EMPTY identity claims for THAT call -- there is
# genuinely nothing to serve yet -- while a background resolution kicks
# off for next time. This is the same fail-closed posture
# decode_jwt_claims's own docstring already establishes for an
# undecodable JWT ("identity present but undecodable" and "identity
# genuinely absent" both correctly deny a role-gated Cedar rule); it is
# not a new, weaker guarantee introduced here. In practice a bearer token
# is presented on many calls across its lifetime, so this only costs the
# very first one per token.
# ---------------------------------------------------------------------------


class OpaqueTokenIntrospector(Protocol):
    """Pluggable resolution point for an opaque bearer token -- implement
    this for whatever your Authorization Server actually exposes (RFC 7662
    introspection, OIDC UserInfo, or a provider-specific endpoint) and pass
    an instance to BackgroundOpaqueTokenResolver. Called ONLY from the
    background worker thread, never from a request path -- a slow or
    blocking implementation here costs nothing but background-resolution
    latency for the NEXT call under the same token, never the current
    one."""

    def introspect(self, token: str) -> dict[str, Any] | None: ...


def _normalize_introspection_claims(raw: Mapping[str, Any]) -> dict[str, Any]:
    """RFC 7662 §2.2's introspection response has exactly ONE field name
    that differs from the vocabulary identity_from_claims()/
    agent_identity_from_claims() already read: `username` where a JWT
    would carry `preferred_username` -- normalized here so both paths
    work UNMODIFIED against either source, no second parallel mapping to
    keep in sync. `client_id` needs NO mapping: it's RFC 7662's own field
    name AND RFC 9068's own required JWT access-token claim AND RFC 8693's
    own definition -- the same standard name in all three specs, already
    checked directly by agent_identity_from_claims(). A no-op for an OIDC
    UserInfo response (OIDC Core §5.3.2's own standard claims already use
    JWT-identical names)."""
    claims = dict(raw)
    if "username" in claims and "preferred_username" not in claims:
        claims["preferred_username"] = claims["username"]
    return claims


@dataclass(slots=True)
class Rfc7662Introspector:
    """RFC 7662 OAuth 2.0 Token Introspection. POSTs `token=<token>` to
    `introspection_endpoint` with client credentials per §2.1's own
    "MAY require authentication" (Basic auth is the common case; a
    resource server that instead needs a different auth scheme should
    implement OpaqueTokenIntrospector itself rather than subclassing this).
    A response with `active: false` (§2.2 -- REQUIRED field) resolves to
    None, not the response body: an inactive/revoked token has no claims
    worth caching, by the spec's own definition of that field, and caching
    an empty result here is indistinguishable downstream from "not yet
    resolved" -- both correctly leave the caller with no identity."""

    introspection_endpoint: str
    client_id: str
    client_secret: str
    timeout_s: float = 5.0

    def introspect(self, token: str) -> dict[str, Any] | None:
        import httpx

        try:
            resp = httpx.post(
                self.introspection_endpoint,
                data={"token": token},
                auth=(self.client_id, self.client_secret),
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception:  # noqa: BLE001 -- background thread; see this module's own
            # "never let a tagging/detection failure break a real call" posture,
            # same as observation.py's on_start()/emit_observed_span(). A failed
            # introspection just means no claims get cached this cycle; the next
            # call under the same token retries.
            log.warning("opaque_token_introspection_failed", exc_info=True)
            return None
        if not isinstance(body, dict) or not body.get("active"):
            return None
        return _normalize_introspection_claims(body)


@dataclass(slots=True)
class OidcUserInfoIntrospector:
    """OIDC Core §5.3 UserInfo Endpoint -- GETs `userinfo_endpoint` with
    the opaque token itself as the bearer credential (§5.3.1: the
    UserInfo Endpoint IS an OAuth 2.0 protected resource, the access
    token IS the credential; no separate client auth). Only ever resolves
    END-USER claims (`sub` plus whatever standard profile claims the
    provider returns) -- UserInfo has no concept of an `act`/delegation
    claim, so agent identity from this path is always empty; use
    Rfc7662Introspector (or a custom OpaqueTokenIntrospector) against a
    provider that supports RFC 8693 token exchange if agent-identity
    resolution for an opaque token matters for your deployment."""

    userinfo_endpoint: str
    timeout_s: float = 5.0

    def introspect(self, token: str) -> dict[str, Any] | None:
        import httpx

        try:
            resp = httpx.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception:  # noqa: BLE001 -- see Rfc7662Introspector's own note.
            log.warning("opaque_token_userinfo_failed", exc_info=True)
            return None
        return body if isinstance(body, dict) else None


def _token_cache_key(token: str) -> str:
    """A stable, non-reversible cache key -- sha256, never the raw token.
    The raw token is held only for the duration of one introspect() call
    on the background thread; it is never itself stored in the cache,
    logged, or otherwise retained past that call, the same discipline this
    whole codebase applies to every other credential (see signing.py/
    pep_identity.py)."""
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(slots=True)
class _CachedClaims:
    claims: dict[str, Any]
    resolved_at: float


class BackgroundOpaqueTokenResolver:
    """Owns one daemon worker thread that resolves opaque tokens via an
    OpaqueTokenIntrospector, OFF the request path -- see this section's own
    module-level comment for the full "why", and control_plane.py's
    run_bundle_poller() for the architecturally identical pattern this
    mirrors (local-cache-read-is-the-hot-path, background-thread-does-the-
    network-call).

    `get_cached()` is the ONLY method a request path should call -- a
    synchronous dict read under a lock, never a network call, so it is
    always fast regardless of introspection endpoint latency or an
    Authorization Server outage. `request_resolution()` enqueues
    background work and returns immediately; never blocks, never raises
    into the caller."""

    def __init__(
        self,
        introspector: OpaqueTokenIntrospector,
        *,
        ttl_s: float = 300.0,
        max_queue: int = 1000,
    ) -> None:
        import queue
        import threading

        self._introspector = introspector
        self._ttl_s = ttl_s
        self._cache: dict[str, _CachedClaims] = {}
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(
            target=self._run, name="parapetai-opaque-token-resolver", daemon=True
        )
        self._thread.start()

    def get_cached(self, token: str) -> dict[str, Any] | None:
        """Synchronous, lock-protected dict read -- the ONLY thing a
        request path (extract()) ever calls. Returns None for a cache miss
        OR an entry past `ttl_s` (treated identically: both mean "resolve
        again", never a stale value silently served past its TTL)."""
        import time

        key = _token_cache_key(token)
        with self._lock:
            entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() - entry.resolved_at > self._ttl_s:
            return None
        return entry.claims

    def request_resolution(self, token: str) -> None:
        """Enqueue `token` for background resolution. Never blocks: a full
        queue, or a token already in flight (another call under the same
        token already enqueued it), silently drops this request rather
        than waiting -- the request path this is called from must never
        stall on this call, and the next call under the same still-
        unresolved token will simply try again."""
        key = _token_cache_key(token)
        with self._lock:
            if key in self._in_flight:
                return
            self._in_flight.add(key)
        try:
            self._queue.put_nowait(token)
        except Exception:  # noqa: BLE001 -- queue.Full, or anything else -- never let
            # enqueueing raise into the caller's own request path.
            with self._lock:
                self._in_flight.discard(key)
            log.warning("opaque_token_resolution_enqueue_failed")

    def _run(self) -> None:
        while True:
            token = self._queue.get()
            key = _token_cache_key(token)
            try:
                claims = self._introspector.introspect(token)
                if claims:
                    import time

                    with self._lock:
                        self._cache[key] = _CachedClaims(claims=claims, resolved_at=time.time())
            except Exception:  # noqa: BLE001 -- a background daemon thread; an uncaught
                # exception here would silently kill all future resolution for
                # the process (the thread just stops), which is worse than one
                # failed resolution -- log and keep the loop alive instead.
                log.warning("opaque_token_resolution_failed", exc_info=True)
            finally:
                with self._lock:
                    self._in_flight.discard(key)
                self._queue.task_done()


@dataclass(slots=True)
class JwtIdentityExtractor:
    """The reference TokenIdentityExtractor -- decodes a JWT bearer token
    into both end-user and (optional) agent identity in one pass. Register
    a different TokenIdentityExtractor for a non-JWT token format -- see
    that Protocol's docstring.

    `opaque_resolver`, when given, is consulted ONLY for a token that
    fails the structural JWT check (decode_jwt_claims returns None) --
    see BackgroundOpaqueTokenResolver's own docstring for the async
    architecture. A cache hit resolves this call's identity for real; a
    cache miss enqueues background resolution (non-blocking) and this
    call still returns an empty ExtractedIdentity, same as the
    no-resolver-configured case always has. Omitted (the default): opaque
    tokens are never resolved at all, unchanged from this class's
    original behavior."""

    opaque_resolver: BackgroundOpaqueTokenResolver | None = None

    def extract(self, token: str) -> ExtractedIdentity:
        claims = decode_jwt_claims(token)
        if claims is not None:
            return _extracted_identity_from_claims(claims)
        if self.opaque_resolver is None:
            return ExtractedIdentity()
        cached = self.opaque_resolver.get_cached(token)
        if cached is not None:
            return _extracted_identity_from_claims(cached)
        self.opaque_resolver.request_resolution(token)
        return ExtractedIdentity()
