"""mTLS client identity, terminated in the gateway.

Three layers, because each catches something the others cannot:

1. certificate parsing (CN extraction);
2. the app's handling of a certificate in the ASGI scope, including that no
   header can stand in for one;
3. a REAL uvicorn server doing a REAL handshake with real certificates, which
   is the only place "the TLS layer refuses an untrusted client" is actually
   true rather than assumed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import parapetai_gateway.server.app as app_module
import pytest
import uvicorn
from fastapi.testclient import TestClient
from parapetai_gateway.identity.bindings import Binding, BindingTable
from parapetai_gateway.identity.mtls import (
    PEER_CERT_SCOPE_KEY,
    _validated_peer_cert,
    common_name,
    uvicorn_tls_kwargs,
)
from parapetai_gateway.identity.resolver import IdentityResolver
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

from .identity_support import Ca, der_of, issue_client_cert, issue_server_cert, make_ca

POLICIES = Path(__file__).resolve().parents[2] / "policies"
_DENIED_TOOL = "execute_shell"
_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": _DENIED_TOOL}}


def _resolver(*, require: bool = False) -> IdentityResolver:
    return IdentityResolver(
        bindings=BindingTable([Binding("mtls", "agent-mtls", cn="fib-sales")]),
        mtls_enabled=True,
        require_verified=require,
    )


def _app(*, require: bool = False) -> Any:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return create_app(engine, identity_resolver=_resolver(require=require))


# ── 1. certificate parsing ───────────────────────────────────────────────


def test_common_name_is_read_from_a_client_certificate(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, "fib-sales")

    assert common_name(der_of(crt)) == "fib-sales"


def test_a_certificate_with_no_cn_has_no_identity(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, None)

    assert common_name(der_of(crt)) is None


def test_a_certificate_with_two_cns_has_no_identity_rather_than_a_guess(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, "fib-sales", extra_cn="fib-admin")

    assert common_name(der_of(crt)) is None


@pytest.mark.parametrize("junk", [b"", b"not a certificate", b"\x30\x03\x02\x01\x01"])
def test_unparseable_bytes_have_no_identity(junk: bytes) -> None:
    assert common_name(junk) is None


class _FakeSslObject:
    def __init__(self, verify_mode: ssl.VerifyMode, der: bytes | None) -> None:
        self.context = type("Ctx", (), {"verify_mode": verify_mode})()
        self._der = der

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        return self._der


class _FakeTransport(asyncio.Transport):
    def __init__(self, ssl_object: Any) -> None:
        super().__init__()
        self._ssl_object = ssl_object

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._ssl_object if name == "ssl_object" else default


def test_a_certificate_is_never_surfaced_from_a_connection_that_is_not_verifying_peers() -> None:
    """Belt and braces: even if the protocol were wired up without
    ssl_cert_reqs, whatever cert a client sent is unvalidated and must not
    become an identity."""
    unverified = _FakeTransport(_FakeSslObject(ssl.CERT_NONE, b"a-cert-anyone-can-send"))
    verified = _FakeTransport(_FakeSslObject(ssl.CERT_REQUIRED, b"a-validated-cert"))

    assert _validated_peer_cert(unverified) is None
    assert _validated_peer_cert(verified) == b"a-validated-cert"


def test_a_plain_http_connection_has_no_certificate() -> None:
    assert _validated_peer_cert(_FakeTransport(None)) is None


def test_mtls_needs_a_server_certificate() -> None:
    with pytest.raises(RuntimeError, match="PARAPETAI_TLS_CERT"):
        uvicorn_tls_kwargs(cert=None, key=None, client_ca="/ca.crt", client_auth="required")


def test_an_unknown_client_auth_mode_stops_startup() -> None:
    with pytest.raises(RuntimeError, match="required"):
        uvicorn_tls_kwargs(cert="/s.crt", key="/s.key", client_ca="/ca.crt", client_auth="maybe")


# ── 2. the app's handling of a certificate in scope ──────────────────────


class _WithPeerCert:
    """Stands in for the mTLS protocol: stamps a certificate onto the scope the
    way PeerCertProtocol does, so the app's handling can be tested without a
    socket."""

    def __init__(self, app: Any, der: bytes | None) -> None:
        self._app, self._der = app, der

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope[PEER_CERT_SCOPE_KEY] = self._der
        await self._app(scope, receive, send)


def _client(der: bytes | None, *, require: bool = False) -> TestClient:
    return TestClient(_WithPeerCert(_app(require=require), der))


def _agent_seen(client: TestClient) -> str | None:
    records = client.get("/__parapetai/observations").json()["records"]
    return records[0]["agent_id"] if records else None


def test_a_bound_certificate_becomes_the_bound_agent(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, "fib-sales")
    client = _client(der_of(crt))

    resp = client.post("/mcp", json=_BODY)

    assert resp.status_code == 403
    assert resp.headers["x-parapetai-decision"] == "deny"
    record = client.get("/__parapetai/observations").json()["records"][0]
    assert record["agent_id"] == "agent-mtls"
    assert record["identity_method"] == "mtls"


def test_a_valid_certificate_with_no_binding_is_forbidden(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, "stranger")
    client = _client(der_of(crt))

    resp = client.post("/mcp", json=_BODY)

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "identity_not_bound"
    assert _agent_seen(client) is None


@pytest.mark.parametrize("require", [False, True])
def test_a_certificate_with_no_usable_cn_is_401_never_a_fallback_to_the_path_claim(
    tmp_path: Path, require: bool
) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, None)
    client = _client(der_of(crt), require=require)

    resp = client.post("/a/fib-sales/mcp", json=_BODY)

    assert resp.status_code == 401
    assert _agent_seen(client) is None


def test_a_garbage_certificate_is_refused() -> None:
    assert _client(b"garbage").post("/mcp", json=_BODY).status_code == 401


def test_a_url_naming_another_agent_than_the_certificate_is_refused(tmp_path: Path) -> None:
    ca = make_ca(tmp_path, "test-ca")
    crt, _ = issue_client_cert(ca, tmp_path, "fib-sales")

    resp = _client(der_of(crt)).post("/a/agent-other/mcp", json=_BODY)

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "identity_path_mismatch"


@pytest.mark.parametrize(
    "spoof",
    [
        {"x-client-cert": "fib-sales"},
        {"x-forwarded-client-cert": 'Subject="CN=fib-sales"'},
        {"ssl-client-s-dn": "CN=fib-sales"},
        {"x-ssl-client-cn": "fib-sales"},
        {PEER_CERT_SCOPE_KEY: "fib-sales"},
        {"x-parapetai-identity": "fib-sales"},
    ],
)
def test_no_header_can_stand_in_for_a_client_certificate(spoof: dict[str, str]) -> None:
    """The point of terminating mTLS in the gateway: identity comes from the
    handshake, not from anything a caller can type. With verification required
    and no real certificate, every one of these is just an unauthenticated
    request."""
    client = _client(None, require=True)

    resp = client.post("/a/agent-mtls/mcp", json=_BODY, headers=spoof)

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "identity_required"
    assert _agent_seen(client) is None


# ── 3. a real TLS handshake ──────────────────────────────────────────────


@dataclasses.dataclass
class Pki:
    ca: Ca
    rogue_ca: Ca
    directory: Path
    server_crt: Path
    server_key: Path

    def client(self, cn: str | None, *, ca: Ca | None = None, name: str = "client") -> httpx.Client:
        """A client presenting a certificate issued by `ca` (default: the trusted one).

        Built from an explicit SSLContext: httpx 0.28's `cert=` argument is
        deprecated and, alongside a `verify=<path>`, does not reliably present
        the certificate -- which would make every "client cert" test here
        silently a "no client cert" test.
        """
        crt, key = issue_client_cert(ca or self.ca, self.directory, cn, name=name)
        context = ssl.create_default_context(cafile=str(self.ca.cert_path))
        context.load_cert_chain(str(crt), str(key))
        return httpx.Client(verify=context, timeout=5)

    def anonymous_client(self) -> httpx.Client:
        return httpx.Client(
            verify=ssl.create_default_context(cafile=str(self.ca.cert_path)), timeout=5
        )


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("pki")
    ca = make_ca(directory, "gateway-client-ca")
    rogue = make_ca(directory, "rogue-ca")
    server_crt, server_key = issue_server_cert(ca, directory)
    return Pki(ca, rogue, directory, server_crt, server_key)


@contextmanager
def _tls_gateway(pki: Pki, client_auth: str, *, require: bool = False) -> Iterator[str]:
    """A real uvicorn server on an ephemeral port, configured exactly as
    server/main.py configures it."""
    config = uvicorn.Config(
        _app(require=require),
        host="127.0.0.1",
        port=0,
        log_level="error",
        **uvicorn_tls_kwargs(
            cert=str(pki.server_crt),
            key=str(pki.server_key),
            client_ca=str(pki.ca.cert_path),
            client_auth=client_auth,
        ),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "test server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"https://localhost:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_client_certificate_from_the_trusted_ca_is_the_identity(pki: Pki) -> None:
    with _tls_gateway(pki, "required") as url, pki.client("fib-sales") as client:
        resp = client.post(f"{url}/mcp", json=_BODY)
        record = client.get(f"{url}/__parapetai/observations").json()["records"][0]

    assert resp.status_code == 403
    assert resp.headers["x-parapetai-decision"] == "deny"  # reached a Cedar decision
    assert record["agent_id"] == "agent-mtls"
    assert record["identity_method"] == "mtls"


def test_a_valid_certificate_with_no_binding_is_forbidden_over_real_tls(pki: Pki) -> None:
    with _tls_gateway(pki, "required") as url, pki.client("stranger", name="s") as client:
        resp = client.post(f"{url}/mcp", json=_BODY)

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "identity_not_bound"


def test_required_mode_refuses_the_handshake_when_no_client_certificate_is_sent(pki: Pki) -> None:
    with _tls_gateway(pki, "required") as url:
        with pki.anonymous_client() as client:
            with pytest.raises(httpx.TransportError):
                client.post(f"{url}/mcp", json=_BODY)


def test_a_certificate_from_an_untrusted_ca_never_reaches_the_app(pki: Pki) -> None:
    """The handshake itself fails: even claiming the bound CN, a certificate the
    configured CA did not sign is not an identity."""
    with _tls_gateway(pki, "optional") as url:
        with pki.client("fib-sales", ca=pki.rogue_ca, name="rogue") as client:
            with pytest.raises(httpx.TransportError):
                client.post(f"{url}/mcp", json=_BODY)


def test_optional_mode_lets_a_certificate_less_caller_through_as_unauthenticated(
    pki: Pki,
) -> None:
    """`optional` exists so JWT-only callers can share the port. A caller with
    no certificate is unauthenticated -- and with verification required, refused
    like any other unauthenticated caller."""
    with _tls_gateway(pki, "optional", require=True) as url:
        with pki.anonymous_client() as client:
            resp = client.post(f"{url}/mcp", json=_BODY)

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "identity_required"


def test_optional_mode_still_identifies_a_caller_who_does_present_a_certificate(pki: Pki) -> None:
    with (
        _tls_gateway(pki, "optional", require=True) as url,
        pki.client("fib-sales", name="opt") as client,
    ):
        resp = client.post(f"{url}/mcp", json=_BODY)
        record = client.get(f"{url}/__parapetai/observations").json()["records"][0]

    assert resp.status_code == 403  # a Cedar decision, not an identity refusal
    assert record["agent_id"] == "agent-mtls"


def test_the_app_module_is_untouched_by_this_suite() -> None:
    # Guards against a test leaking a monkeypatched module-level `settings`.
    assert app_module.settings.tls_client_ca is None or isinstance(
        app_module.settings.tls_client_ca, str
    )
