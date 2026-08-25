"""Bridges an implementor's own already-authenticated request state into
parapetai_agent.maf.current_identity()'s ambient context, automatically, for
every request -- so agent.run() calls anywhere in a handler are governed
by the signed-in caller's identity without wrapping each one in
`with current_identity(...):` by hand.

Requires the optional `web` extra (`pip install parapetai-agent[web]`) --
Starlette is a real dependency of THIS module only. parapetai_agent's core
(build_middleware()/GovernedAgent) must stay usable by a non-web consumer
(a CLI script, a background worker) without forcing a web-framework
dependency on it, matching CLAUDE.md's "interop is optional" stance
already applied to the `maf` extra. Importing this module without
`web` installed raises a normal ImportError at the `import starlette`
line, not a silent no-op.

## What this can't do, and why

Full automatic identity detection -- zero implementor involvement at all
-- is not possible. Verified directly, not assumed (parapetai_agent/maf.py's
own docstring): Microsoft Agent Framework's AgentSession carries only
session_id/service_session_id, nothing identity-shaped, and every IdP's
claims format differs, so *something* has to decode a raw token/session
into Cedar's (identity_claims, identity_roles) shape. That decoding step
(parapetai_agent.token_identity.identity_from_claims(), typically called once
inside an implementor's own /auth/callback route) is unavoidable and
stays exactly as it is today.

What THIS class removes is the *per-call-site* burden of re-entering
current_identity() around every single agent.run(). Wired once
(`app.add_middleware(IdentityMiddleware)`), not per route, not per call.

For the common case -- identity IS a JWT bearer token on a request header
or cookie -- jwt_bearer_extractor() below is a ready-made extractor= that
does that decoding step, so most callers never write claims-mapping code
at all: `IdentityMiddleware(app, extractor=jwt_bearer_extractor())`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from parapetai_agent.maf import current_identity
from parapetai_agent.token_identity import decode_jwt_claims, identity_from_claims

Extractor = Callable[[Request], tuple[Mapping[str, Any], Sequence[str]] | None]


def jwt_bearer_extractor(
    *, header: str | None = "Authorization", cookie: str | None = None, scheme: str = "Bearer"
) -> Extractor:
    """Ready-made extractor= for the common case: identity is a JWT on an
    inbound request header (default: Authorization: Bearer <token>) or
    cookie -- state where the token lives, this reads and decodes it, no
    manual claims-mapping code needed at the call site:

        app.add_middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())

    Reuses token_identity.decode_jwt_claims()/identity_from_claims() --
    the same code parapetai_agent.maf.identity_from_bearer_token() already
    uses for MCP's OWN outbound bearer tokens, so an inbound HTTP token
    and an outbound MCP token decode through one implementation, not two.

    Pass header=None, cookie="..." to read from a cookie instead. scheme
    is stripped as a "<scheme> " prefix when reading from a header (set
    scheme="" if the header value is the bare token with no prefix) --
    ignored when reading from a cookie, since a cookie value is never
    prefixed that way.

    Not a claim this verifies the token's signature -- see
    token_identity.py's module docstring, "Signature verification"
    section, for exactly what decode_jwt_claims() does and doesn't prove.
    That's the same, already-justified tradeoff identity_from_bearer_token()
    makes for the outbound-MCP case; applying it to an INBOUND caller here
    is only appropriate if something upstream (a real gateway, TLS-terminated
    auth) already validated the token before this process saw it -- this
    function doesn't introduce a new tradeoff, it reuses the existing one.
    """

    def _extract(request: Request) -> tuple[Mapping[str, Any], Sequence[str]] | None:
        raw: str | None = None
        if header:
            raw = request.headers.get(header)
            if raw and scheme and raw.startswith(f"{scheme} "):
                raw = raw[len(scheme) + 1 :]
        elif cookie:
            raw = request.cookies.get(cookie)
        if not raw:
            return None
        claims = decode_jwt_claims(raw)
        if claims is None:
            return None
        end_user_claims, end_user_roles = identity_from_claims(claims)
        if not end_user_claims and not end_user_roles:
            return None
        return end_user_claims, end_user_roles

    return _extract


class IdentityMiddleware(BaseHTTPMiddleware):
    """Two ways to tell it where identity lives, both wired once:

    1. Zero-config default: reads request.state.<claims_attr> /
       request.state.<roles_attr> (default names "identity_claims" /
       "identity_roles") if present -- the convention several Starlette
       auth patterns (e.g. AuthenticationMiddleware) already populate
       request.state with.
    2. `extractor=` callable, for an implementor whose identity lives
       somewhere else entirely (e.g. their own cookie-keyed session
       dict, as in examples/maf_webapp/web_app.py) --
       NOT a callback contract this package imposes on their auth flow;
       their /login and /auth/callback routes are untouched, still
       whatever IdP client they already use. The extractor is a private,
       few-line adapter reading their own already-existing state,
       supplied once here, not a route handler shape this package
       dictates.

    Absent identity (no claims, no roles, extractor returns None) means
    current_identity() is simply never entered for that request -- the
    same Agent::"anonymous" default-deny behavior as calling agent.run()
    with no ambient identity at all today. This middleware never denies
    a request itself; it only supplies context Cedar policy then
    evaluates, same as always.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        claims_attr: str = "identity_claims",
        roles_attr: str = "identity_roles",
        extractor: Extractor | None = None,
    ) -> None:
        super().__init__(app)
        self._claims_attr = claims_attr
        self._roles_attr = roles_attr
        self._extractor = extractor

    def _resolve(self, request: Request) -> tuple[Mapping[str, Any], Sequence[str]] | None:
        if self._extractor is not None:
            return self._extractor(request)
        claims = getattr(request.state, self._claims_attr, None)
        roles = getattr(request.state, self._roles_attr, None)
        if not claims and not roles:
            return None
        return claims or {}, roles or []

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        resolved = self._resolve(request)
        if resolved is None:
            result: Awaitable[Response] = call_next(request)
            return await result
        claims, roles = resolved
        with current_identity(claims=dict(claims), roles=list(roles)):
            return await call_next(request)
