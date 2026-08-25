"""IdentityMiddleware -- proves the two documented ways to tell it where
identity lives (zero-config request.state.* attrs vs extractor=) both
correctly enter current_identity() around a request, that no identity
found means no current_identity() is entered at all (same anonymous
default as never wiring the middleware), and that identity from one
request doesn't leak into the next.

Run: uv run --with starlette pytest parapetai-agent/tests/test_identity_middleware.py -v
"""

from __future__ import annotations

import base64
import json

from parapetai_agent.identity_middleware import IdentityMiddleware, jwt_bearer_extractor
from parapetai_agent.identity_store import IdentityKeyKind, get_identity, set_identity
from parapetai_agent.maf import _identity_claims, _identity_roles
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def _observed(request: Request) -> JSONResponse:
    return JSONResponse({"claims": _identity_claims(None), "roles": _identity_roles(None)})


class _SeedRequestState(BaseHTTPMiddleware):
    """Stands in for whatever already-existing auth middleware an
    implementor has that populates request.state -- IdentityMiddleware's
    zero-config default reads exactly these two attribute names."""

    async def dispatch(self, request, call_next):
        request.state.identity_claims = {"sub": "alice"}
        request.state.identity_roles = ["OrderViewer"]
        return await call_next(request)


def test_zero_config_reads_request_state_when_present() -> None:
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(_SeedRequestState), Middleware(IdentityMiddleware)],
    )
    resp = TestClient(app).get("/")
    assert resp.json() == {"claims": {"sub": "alice"}, "roles": ["OrderViewer"]}


def test_zero_config_absent_state_stays_anonymous() -> None:
    app = Starlette(routes=[Route("/", _observed)], middleware=[Middleware(IdentityMiddleware)])
    resp = TestClient(app).get("/")
    assert resp.json() == {"claims": {}, "roles": []}


def test_extractor_reads_from_wherever_the_implementor_puts_it() -> None:
    # Mirrors web_app.py's real _extract_identity(): identity lives in an
    # implementor-owned session store keyed by a cookie, not request.state.
    sessions = {"sess-1": {"identity_claims": {"sub": "bob"}, "identity_roles": ["Admin"]}}

    def extractor(request: Request):
        session = sessions.get(request.cookies.get("session_id"))
        if session is None or not session["identity_claims"]:
            return None
        return session["identity_claims"], session["identity_roles"]

    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=extractor)],
    )
    client = TestClient(app)
    client.cookies.set("session_id", "sess-1")
    resp = client.get("/")
    assert resp.json() == {"claims": {"sub": "bob"}, "roles": ["Admin"]}


def test_extractor_no_match_stays_anonymous() -> None:
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=lambda request: None)],
    )
    resp = TestClient(app).get("/")
    assert resp.json() == {"claims": {}, "roles": []}


def test_identity_does_not_leak_across_requests() -> None:
    def extractor(request: Request):
        if request.cookies.get("session_id") != "signed-in":
            return None
        return {"sub": "carol"}, []

    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=extractor)],
    )
    signed_in_client = TestClient(app)
    signed_in_client.cookies.set("session_id", "signed-in")
    signed_in = signed_in_client.get("/")
    anonymous = TestClient(app).get("/")
    assert signed_in.json() == {"claims": {"sub": "carol"}, "roles": []}
    assert anonymous.json() == {"claims": {}, "roles": []}


def test_extractor_composes_directly_with_the_identity_store() -> None:
    # get_identity() already returns the tuple[Mapping, Sequence] | None
    # shape Extractor expects -- proves that composition needs no adapter.
    set_identity("sess-2", kind=IdentityKeyKind.SESSION, claims={"sub": "dave"}, roles=["Admin"])
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[
            Middleware(
                IdentityMiddleware,
                extractor=lambda r: get_identity(
                    r.cookies.get("session_id"), kind=IdentityKeyKind.SESSION
                ),
            )
        ],
    )
    client = TestClient(app)
    client.cookies.set("session_id", "sess-2")
    resp = client.get("/")
    assert resp.json() == {"claims": {"sub": "dave"}, "roles": ["Admin"]}


def _fake_jwt(claims: dict) -> str:
    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


def test_jwt_bearer_extractor_decodes_a_real_shaped_token() -> None:
    token = _fake_jwt({"oid": "abc", "roles": ["OrderViewer"]})
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())],
    )
    resp = TestClient(app).get("/", headers={"Authorization": f"Bearer {token}"})
    assert resp.json() == {"claims": {"oid": "abc"}, "roles": ["OrderViewer"]}


def test_jwt_bearer_extractor_missing_header_stays_anonymous() -> None:
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())],
    )
    resp = TestClient(app).get("/")
    assert resp.json() == {"claims": {}, "roles": []}


def test_jwt_bearer_extractor_malformed_token_stays_anonymous() -> None:
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[Middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())],
    )
    resp = TestClient(app).get("/", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.json() == {"claims": {}, "roles": []}


def test_jwt_bearer_extractor_reads_from_a_cookie_when_configured() -> None:
    token = _fake_jwt({"sub": "erin"})
    app = Starlette(
        routes=[Route("/", _observed)],
        middleware=[
            Middleware(
                IdentityMiddleware,
                extractor=jwt_bearer_extractor(header=None, cookie="id_token"),
            )
        ],
    )
    client = TestClient(app)
    client.cookies.set("id_token", token)
    resp = client.get("/")
    assert resp.json() == {"claims": {"sub": "erin"}, "roles": []}
