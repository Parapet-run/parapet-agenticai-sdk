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

Two real, verified standards, checked in this order by
agent_identity_from_claims():

1. RFC 8693 (OAuth 2.0 Token Exchange)'s `act` (actor) claim -- the formal
   standard for representing a delegation chain in a JWT. `act` is a
   nested JSON object; `act.sub` identifies the current actor (the
   agent/service acting on the subject's behalf). A further-nested
   `act.act` would represent a prior actor in a longer chain, but per the
   RFC, only the OUTERMOST act matters for access-control decisions --
   nested ones are informational. Confirmed directly from RFC 8693, not
   assumed.
2. `azp` (OIDC Core's "Authorized Party" claim) or `appid` (the same
   concept in Entra ID v1.0 tokens, which predate azp) -- the client ID
   the token was issued to. Common in practice even without a full RFC
   8693 token-exchange round trip: an Entra delegated-permission access
   token typically carries the calling app's client ID here alongside the
   user's own oid/sub.

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


# Standard/Entra claim names for the END USER -- oid/tid (Entra), sub
# (OIDC standard subject), preferred_username/upn (Entra display name
# variants), email. Not an exhaustive OIDC claim list by design: these are
# the ones with a clear, stable identity meaning across the tokens this
# repo has actually verified (see docs/maf-in-process-integration.md).
_END_USER_CLAIM_KEYS = ("oid", "sub", "tid", "preferred_username", "upn", "email")

# Claim names checked, in order, for the AGENT/actor's own identity within
# act -- oid/sub/tid because Entra's act claim (when present) shapes
# itself the same way as the outer token's own claims.
_AGENT_CLAIM_KEYS = ("oid", "sub", "tid")


def _claims_subset(claims: Mapping[str, Any], keys: Sequence[str]) -> dict[str, str]:
    return {k: str(claims[k]) for k in keys if claims.get(k) is not None}


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
    section for exactly which two standards this checks and why, in order:
    RFC 8693's `act` claim, then azp/appid.

    Returns None -- not an empty-but-present ExtractedIdentity -- when
    neither is in `claims`. That's the common case for a plain end-user
    token with no delegation involved, not an error condition."""
    act = claims.get("act")
    if isinstance(act, dict) and act:
        return _claims_subset(act, _AGENT_CLAIM_KEYS), []
    for key in ("azp", "appid"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return {"client_id": value}, []
    return None


@dataclass(slots=True)
class JwtIdentityExtractor:
    """The reference TokenIdentityExtractor -- decodes a JWT bearer token
    into both end-user and (optional) agent identity in one pass. Register
    a different TokenIdentityExtractor for a non-JWT token format -- see
    that Protocol's docstring."""

    def extract(self, token: str) -> ExtractedIdentity:
        claims = decode_jwt_claims(token)
        if claims is None:
            return ExtractedIdentity()
        end_user_claims, end_user_roles = identity_from_claims(claims)
        agent = agent_identity_from_claims(claims)
        agent_claims, agent_roles = agent if agent is not None else ({}, [])
        return ExtractedIdentity(
            end_user_claims=end_user_claims,
            end_user_roles=end_user_roles,
            agent_claims=agent_claims,
            agent_roles=agent_roles,
        )
