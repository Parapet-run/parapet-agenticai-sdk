"""Test-only builders for the identity suites: RSA/JWKS/JWT material and a
throwaway PKI. Nothing here ships."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://parapet-gateway"
JWKS_URL = "https://login.microsoftonline.com/tenant-1/discovery/v2.0/keys"

# ── JWT / JWKS ───────────────────────────────────────────────────────────


def new_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(
    key: Any,
    *,
    kid: str | None = "k1",
    alg: str = "RS256",
    claims: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "azp": "app-1",
        **(claims or {}),
    }
    for name in drop:
        payload.pop(name, None)
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def jwk_for(private_key: rsa.RSAPrivateKey, kid: str, **extra: Any) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256", **extra}


def hs256_confusion_token(private_key: rsa.RSAPrivateKey, kid: str) -> str:
    """The classic algorithm-confusion forgery: an HS256 token whose HMAC secret
    is the IdP's PUBLIC key bytes. Hand-assembled because PyJWT itself refuses
    to sign this -- an attacker would not use PyJWT."""
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    body = b64(
        json.dumps(
            {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "azp": "app-1"}
        ).encode()
    )
    signing_input = f"{header}.{body}".encode()
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{b64(signature)}"


# ── PKI ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate
    cert_path: Path


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _builder(subject: x509.Name, issuer: x509.Name, public_key: Any) -> x509.CertificateBuilder:
    now = dt.datetime.now(dt.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
    )


def _authority_key_id(ca: Ca) -> x509.AuthorityKeyIdentifier:
    # Python 3.13+ verifies with X509_STRICT, which refuses a certificate that
    # does not name its issuer's key. Real CAs always set this.
    return x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key())


def make_ca(directory: Path, cn: str) -> Ca:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(cn)
    cert = (
        _builder(name, name, key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = directory / f"{cn}.crt"
    path.write_bytes(_pem(cert))
    return Ca(key, cert, path)


def issue_server_cert(ca: Ca, directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        _builder(_name("localhost"), ca.cert.subject, key.public_key())
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(_authority_key_id(ca), critical=False)
        .sign(ca.key, hashes.SHA256())
    )
    crt, key_path = directory / "server.crt", directory / "server.key"
    crt.write_bytes(_pem(cert))
    key_path.write_bytes(_key_pem(key))
    return crt, key_path


def issue_client_cert(
    ca: Ca, directory: Path, cn: str | None, *, extra_cn: str | None = None, name: str = "client"
) -> tuple[Path, Path]:
    """A client-auth certificate. `cn=None` issues one with no CN;
    `extra_cn` adds a SECOND CN (an ambiguous identity)."""
    key = ec.generate_private_key(ec.SECP256R1())
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, c) for c in (cn, extra_cn) if c]
    attrs = attrs or [x509.NameAttribute(NameOID.ORGANIZATION_NAME, "no-cn")]
    cert = (
        _builder(x509.Name(attrs), ca.cert.subject, key.public_key())
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(_authority_key_id(ca), critical=False)
        .sign(ca.key, hashes.SHA256())
    )
    crt, key_path = directory / f"{name}.crt", directory / f"{name}.key"
    crt.write_bytes(_pem(cert))
    key_path.write_bytes(_key_pem(key))
    return crt, key_path


def der_of(cert_path: Path) -> bytes:
    return x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(
        serialization.Encoding.DER
    )
