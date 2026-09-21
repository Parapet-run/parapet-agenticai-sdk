"""What this gateway reports about itself to the control plane.

A gateway is a policy enforcement point like any other: it heartbeats, so it
appears in the fleet. What that heartbeat cannot say on its own is what makes a
gateway different -- which certificates it is serving, whether a rotation
happened or failed, and which agents are actually connecting through it. This
collects that into one small block, sent as the heartbeat's optional `details`.

Content-free by construction, like the decision audit (invariant 10): agent ids,
identity methods, certificate metadata and counts. Never a request, a prompt, a
tool argument, a token, or any private key material (the key inside a combined
PEM is never parsed, only the certificates around it).

Bounded: the caller-supplied `/a/{agent_id}` path claim is attacker-controlled,
so the table of recently seen identities is an LRU with a hard cap and a length
cap on every string. Flooding it with made-up ids evicts the oldest entries; it
cannot grow memory without limit.

Nothing here is site-specific. `site` is an opaque label the deployment supplies
(PARAPETAI_GATEWAY_SITE) so a control plane fronting several gateways can tell
them apart.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1
_MAX_TEXT = 128
_CERT_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)


def _clip(value: object) -> str:
    return str(value)[:_MAX_TEXT]


@dataclass(slots=True)
class _Seen:
    agent_id: str
    method: str
    subject: str | None
    first_seen: float
    last_seen: float
    requests: int = 0
    denied: int = 0
    held: int = 0


def describe_certificates(pem: bytes) -> list[dict[str, Any]]:
    """Metadata for every CERTIFICATE block in a PEM file, nothing else.

    Reads only the certificate blocks: a combined PEM may also hold a private
    key, and this never parses (or returns) it. An unparseable block is skipped
    rather than failing the report -- status must never take the gateway down.
    """
    described: list[dict[str, Any]] = []
    for block in _CERT_BLOCK.findall(pem):
        try:
            cert = x509.load_pem_x509_certificate(block)
        except ValueError:
            continue
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        issuer = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
        der = cert.public_bytes(serialization.Encoding.DER)
        described.append(
            {
                "subject": _clip(cn[0].value) if cn else None,
                "issuer": _clip(issuer[0].value) if issuer else None,
                "serial": format(cert.serial_number, "x")[:64],
                "sha256": hashlib.sha256(der).hexdigest(),
                "not_before": cert.not_valid_before_utc.timestamp(),
                "not_after": cert.not_valid_after_utc.timestamp(),
            }
        )
    return described


class GatewayStatus:
    def __init__(
        self,
        *,
        site: str | None = None,
        window_s: float = 900.0,
        max_identities: int = 500,
        max_events: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.site = _clip(site) if site else None
        self._window_s = window_s
        self._max_identities = max_identities
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, str], _Seen] = OrderedDict()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._refusals: dict[str, int] = {}
        self._started_at = clock()
        self._config: dict[str, Any] = {}
        self._tls_files: tuple[Path, Path | None, Path] | None = None
        self._tls_state: dict[str, Any] = {}

    # -- recording (hot path: cheap, bounded, never raises into a request) ------

    def record_request(
        self, *, agent_id: str, method: str, subject: str | None, effect: str
    ) -> None:
        now = self._clock()
        key = (_clip(agent_id), _clip(method))
        with self._lock:
            entry = self._seen.pop(key, None)
            if entry is None:
                entry = _Seen(key[0], key[1], _clip(subject) if subject else None, now, now)
            entry.last_seen = now
            entry.requests += 1
            if effect == "deny":
                entry.denied += 1
            elif effect == "review":
                entry.held += 1
            self._seen[key] = entry  # most recently used goes last
            while len(self._seen) > self._max_identities:
                self._seen.popitem(last=False)  # evict the least recently seen

    def record_identity_refusal(self, code: str) -> None:
        """A credential was presented and refused. Counted by reason only: the
        caller is by definition unverified, so nothing about them is recorded."""
        with self._lock:
            key = _clip(code)
            if key in self._refusals or len(self._refusals) < 32:
                self._refusals[key] = self._refusals.get(key, 0) + 1

    def add_event(self, event_type: str, **fields: Any) -> None:
        with self._lock:
            self._events.append(
                {"ts": self._clock(), "type": _clip(event_type)}
                | {k: _clip(v) if isinstance(v, str) else v for k, v in fields.items()}
            )

    # -- configuration -------------------------------------------------------------

    def describe_config(self, **config: Any) -> None:
        self._config = config

    def watch_tls(self, cert: Path, key: Path | None, client_ca: Path) -> None:
        self._tls_files = (cert, key, client_ca)
        self.refresh_tls()

    def refresh_tls(self, **state: Any) -> None:
        """Re-read the certificate metadata from disk. Called at start and after
        each reload, so the report always describes what is actually loaded."""
        if self._tls_files is None:
            return
        cert, _key, client_ca = self._tls_files
        try:
            server = describe_certificates(cert.read_bytes())
            cas = describe_certificates(client_ca.read_bytes())
        except OSError:
            server, cas = [], []  # mid-rotation: keep the last good report below
        with self._lock:
            if server:
                self._tls_state["server_cert"] = server[0]
                self._tls_state["server_chain_length"] = len(server)
            if cas:
                self._tls_state["client_ca"] = cas
            self._tls_state.update(state)

    def on_tls_event(self, event_type: str, details: dict[str, Any]) -> None:
        """The TlsReloader's event hook: records the event and keeps the TLS block
        describing what is actually LOADED."""
        self.add_event(event_type, **details)
        if event_type == "tls_reloaded":
            # New material is live: re-read it so the report describes it.
            self.refresh_tls(
                generation=details.get("generation"), last_reload_at=self._clock(), last_error=None
            )
        elif event_type == "tls_reload_failed":
            # The previous material is still what is served. Do NOT re-read the
            # files: they hold the rejected material.
            self.record_tls_error(str(details.get("error", "")))

    def record_tls_error(self, error: str) -> None:
        """A rotation was rejected. Only the error is recorded: what is loaded, and
        so what is reported, has not changed."""
        with self._lock:
            self._tls_state["last_error"] = _clip(error)

    # -- reporting -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            connected = [
                {
                    "agent_id": s.agent_id,
                    "method": s.method,
                    "subject": s.subject,
                    "first_seen": s.first_seen,
                    "last_seen": s.last_seen,
                    "requests": s.requests,
                    "denied": s.denied,
                    "held_for_review": s.held,
                }
                for s in reversed(self._seen.values())
                if now - s.last_seen <= self._window_s
            ]
            report: dict[str, Any] = {
                "kind": "gateway",
                "schema": SCHEMA_VERSION,
                "site": self.site,
                "started_at": self._started_at,
                "reported_at": now,
                "connected_window_s": self._window_s,
                "config": dict(self._config),
                "identity_refusals": dict(self._refusals),
                "connected": connected,
                "recent_events": list(self._events),
            }
            if self._tls_files is not None:
                report["tls"] = dict(self._tls_state)
            return report
