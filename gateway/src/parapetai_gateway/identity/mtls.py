"""mTLS client identity, terminated in the gateway.

uvicorn completes the TLS handshake but does not hand the peer certificate to
the ASGI app, so this module adds the one missing piece: a protocol subclass
that reads the certificate off the live TLS connection and places it in the
ASGI scope.

WHY THIS CAN BE TRUSTED. The certificate arrives in `scope`, which uvicorn
builds itself from the connection -- a caller cannot set a scope key, only
headers, and no header is ever read as a certificate. It is only present at all
if the handshake already validated it against `ssl_ca_certs`
(`ssl.CERT_REQUIRED` / `CERT_OPTIONAL` both abort the handshake for a
certificate that does not chain to the configured CA). As belt and braces the
protocol refuses to surface a certificate from a connection whose SSL context
is not verifying peers, so wiring this protocol up without `ssl_cert_reqs`
cannot yield an unvalidated identity.

Behind a TLS-terminating ingress the gateway sees plain HTTP and none of this
applies -- use the JWT method there. Do NOT forward a certificate or its CN in
a header and trust it: that is exactly the spoofable channel this design
avoids.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID
from uvicorn.protocols.http.h11_impl import H11Protocol

# Namespaced so it cannot collide with a key another ASGI component sets.
PEER_CERT_SCOPE_KEY = "parapetai.peer_cert_der"


def common_name(der: bytes) -> str | None:
    """The certificate's subject CN, or None if it has none, has several, or
    cannot be parsed. Several is refused rather than picking one: with two CNs
    "which is the identity" has no defensible answer, and guessing wrong maps
    a caller onto someone else's agent."""
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError:
        return None
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(attrs) != 1:
        return None
    value = attrs[0].value
    return value if isinstance(value, str) and value else None


def _validated_peer_cert(transport: asyncio.BaseTransport) -> bytes | None:
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    if ssl_object.context.verify_mode == ssl.CERT_NONE:
        return None  # not verifying peers: whatever it holds is unvalidated
    der: bytes | None = ssl_object.getpeercert(binary_form=True)
    return der


class _PeerCertApp:
    """Wraps the ASGI app for ONE connection, stamping the (validated) peer
    certificate -- or None -- onto every request's scope. It always
    overwrites the key, so a value can never survive from anywhere else."""

    def __init__(self, app: Any, transport: asyncio.BaseTransport) -> None:
        self._app = app
        self._transport = transport

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[PEER_CERT_SCOPE_KEY] = _validated_peer_cert(self._transport)
        await self._app(scope, receive, send)


class PeerCertProtocol(H11Protocol):
    """uvicorn's h11 HTTP protocol, plus the peer certificate in the scope."""

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self.app = _PeerCertApp(self.app, transport)


def uvicorn_tls_kwargs(
    *, cert: str | None, key: str | None, client_ca: str, client_auth: str
) -> dict[str, Any]:
    """`uvicorn.run()` keyword arguments that enable mTLS. Raises rather than
    starting a half-configured listener: a server that silently ran without
    client verification would look identical to one that worked."""
    if not cert:
        raise RuntimeError(
            "PARAPETAI_TLS_CLIENT_CA enables mTLS, which also needs PARAPETAI_TLS_CERT (the "
            "gateway's own server certificate) and PARAPETAI_TLS_KEY -- the key may be omitted "
            "only when the certificate file also contains the private key, as some "
            "secrets managers export it"
        )
    modes = {"required": ssl.CERT_REQUIRED, "optional": ssl.CERT_OPTIONAL}
    if client_auth not in modes:
        raise RuntimeError(
            f"PARAPETAI_TLS_CLIENT_AUTH must be 'required' or 'optional' (got {client_auth!r})"
        )
    return {
        "ssl_certfile": cert,
        "ssl_keyfile": key,
        "ssl_ca_certs": client_ca,
        "ssl_cert_reqs": modes[client_auth],
        "http": PeerCertProtocol,
    }
