"""Minimal OAuth 2.1 authorization server + Dynamic Client Registration
(RFC 7591), scoped to exactly what an MCP client that enforces the MCP
Authorization spec requires in front of a remote MCP server it doesn't
operate itself -- e.g. Atlassian Rovo's "add an external MCP server" flow,
which requires Streamable HTTP + OAuth 2.1 authorization_code+PKCE + DCR
before it will register a custom tool source at all.

WHAT THIS IS NOT: a multi-tenant identity provider. There is no per-user
account system here -- `/authorize` is gated by ONE deployment-operator
secret (PARAPETAI_MCP_OAUTH_SHARED_SECRET), checked once per authorization
grant. That is deliberately sufficient: Cedar is the real authorization
decision in this gateway regardless of OAuth identity (`proxy()`'s own
docstring: "agent_id is a caller-supplied claim, not a verified credential" --
true with or without this module). This module's only job is satisfying the
*protocol* handshake a third-party MCP client requires, not re-deriving
Parapet's authorization model on top of OAuth subjects.

State is in-memory and process-local by design -- the same "single-instance,
not multi-replica" constraint parapet-platform's own control-plane/maf-webapp
Azure deploys already apply to their SQLite file, for the identical reason
(no distributed lock). A gateway running with mcp_auth_mode=oauth2 must run
maxReplicas=1 until this moves to a shared store; see this repo's own
gateway/deploy/azure/README.md.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

# ── storage ──────────────────────────────────────────────────────────
# Process-local dicts, not a database -- see module docstring. Codes and
# tokens are opaque, high-entropy (secrets.token_urlsafe), and short-lived;
# nothing here is a JWT because nothing downstream ever needs to introspect
# these tokens' claims -- validate_bearer() is the only consumer, in-process.

_CLIENTS: dict[str, ClientRecord] = {}
_CODES: dict[str, AuthCode] = {}
_TOKENS: dict[str, TokenRecord] = {}


@dataclass(frozen=True, slots=True)
class ClientRecord:
    client_id: str
    redirect_uris: tuple[str, ...]
    client_name: str


@dataclass(frozen=True, slots=True)
class AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    expires_at: float
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class TokenRecord:
    client_id: str
    expires_at: float


class OAuthError(Exception):
    """Maps directly to an OAuth 2.1 error response body.

    `status` is the HTTP status to return; `error`/`error_description` are
    the RFC 6749 §5.2 fields every OAuth client already knows how to parse.
    """

    def __init__(self, error: str, description: str, *, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def to_body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def reset_state_for_tests() -> None:
    """Test-only: OAuth state is module-level, so parallel tests would
    otherwise bleed into each other. Never called from production code."""
    _CLIENTS.clear()
    _CODES.clear()
    _TOKENS.clear()


# ── metadata (RFC 8414 / RFC 9728) ──────────────────────────────────────


def protected_resource_metadata(base_url: str, resource_url: str) -> dict[str, Any]:
    """RFC 9728 Protected Resource Metadata -- what the 401's
    WWW-Authenticate `resource_metadata` parameter points at.

    `resource_url` is the exact downstream-facing URL this document
    describes (e.g. "{base}/mcp" for the single-upstream case, or
    "{base}/a/{agent}/mcp/{target}" per RFC 8615's path-mapped well-known
    convention when more than one downstream MCP server is fronted -- see
    app.py's oauth_protected_resource_for()). One authorization server
    (this gateway) protects every resource it fronts, regardless of how
    many; DCR/token issuance is shared and global, only this document
    varies per resource."""
    return {
        "resource": resource_url,
        "authorization_servers": [base_url],
    }


def authorization_server_metadata(base_url: str) -> dict[str, Any]:
    """RFC 8414 Authorization Server Metadata. `token_endpoint_auth_methods_supported`
    is "none" only: every client here is a public client authenticated by
    PKCE, never a client_secret -- OAuth 2.1 requires PKCE for every
    authorization_code grant regardless, so there is no weaker path to
    disable."""
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


# ── Dynamic Client Registration (RFC 7591) ──────────────────────────────


def register_client(body: dict[str, Any]) -> dict[str, Any]:
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError("invalid_client_metadata", "redirect_uris is required and non-empty")
    if not all(isinstance(uri, str) and uri for uri in redirect_uris):
        raise OAuthError("invalid_client_metadata", "redirect_uris must be non-empty strings")

    client_id = secrets.token_urlsafe(24)
    client_name = str(body.get("client_name") or "unnamed-mcp-client")
    record = ClientRecord(
        client_id=client_id, redirect_uris=tuple(redirect_uris), client_name=client_name
    )
    _CLIENTS[client_id] = record
    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": list(record.redirect_uris),
        "client_name": record.client_name,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


# ── authorization_code + PKCE ────────────────────────────────────────────


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start_authorization(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str | None,
    code_challenge_method: str | None,
    response_type: str | None,
) -> None:
    """Validates an /authorize request's params. Raises OAuthError on any
    failure that must NOT redirect back to redirect_uri (per RFC 6749 §4.1.2.1
    -- an invalid/unregistered redirect_uri or unknown client is never
    reflected back to attacker-controlled redirect_uri; every other error
    IS returned via redirect, which the caller handles separately)."""
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "only response_type=code is supported")
    client = _CLIENTS.get(client_id)
    if client is None:
        raise OAuthError("invalid_client", "unknown client_id", status=401)
    if redirect_uri not in client.redirect_uris:
        raise OAuthError("invalid_request", "redirect_uri does not match a registered value")
    if code_challenge_method != "S256":
        raise OAuthError(
            "invalid_request", "code_challenge_method must be S256 (OAuth 2.1 mandates PKCE)"
        )
    if not code_challenge:
        raise OAuthError("invalid_request", "code_challenge is required")


def issue_code(*, client_id: str, redirect_uri: str, code_challenge: str, ttl_s: float) -> str:
    code = secrets.token_urlsafe(32)
    _CODES[code] = AuthCode(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        expires_at=time.monotonic() + ttl_s,
    )
    return code


def exchange_token(
    *,
    grant_type: str | None,
    code: str | None,
    redirect_uri: str | None,
    client_id: str | None,
    code_verifier: str | None,
    ttl_s: float,
) -> dict[str, Any]:
    if grant_type != "authorization_code":
        raise OAuthError("unsupported_grant_type", "only authorization_code is supported")
    if not code or not client_id or not redirect_uri or not code_verifier:
        raise OAuthError(
            "invalid_request", "code, redirect_uri, client_id, code_verifier are required"
        )

    auth_code = _CODES.get(code)
    if auth_code is None or auth_code.consumed:
        raise OAuthError("invalid_grant", "unknown, expired, or already-used code")
    if auth_code.expires_at < time.monotonic():
        del _CODES[code]
        raise OAuthError("invalid_grant", "code expired")
    if auth_code.client_id != client_id or auth_code.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "code was not issued to this client/redirect_uri")

    expected_challenge = _b64url_no_pad(hashlib.sha256(code_verifier.encode()).digest())
    if not secrets.compare_digest(expected_challenge, auth_code.code_challenge):
        raise OAuthError("invalid_grant", "code_verifier does not match code_challenge")

    # Single-use: consume immediately so a replayed code (e.g. a client
    # retrying a slow response) can never mint a second token.
    del _CODES[code]

    access_token = secrets.token_urlsafe(32)
    _TOKENS[access_token] = TokenRecord(client_id=client_id, expires_at=time.monotonic() + ttl_s)
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": int(ttl_s)}


def validate_bearer(authorization_header: str | None) -> bool:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    token = authorization_header[len("Bearer ") :].strip()
    record = _TOKENS.get(token)
    if record is None:
        return False
    if record.expires_at < time.monotonic():
        del _TOKENS[token]
        return False
    return True
