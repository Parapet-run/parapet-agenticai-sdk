"""Hot-reload of the gateway's mTLS material (server certificate, key, client CA).

Why this exists: with certificates issued from a secrets manager (mounted by a
secrets-store CSI driver, written by cert-manager, ...) the files on disk are
replaced when a certificate renews or a CA rotates. uvicorn reads them once at
startup, so without this a renewal means a restart, and a certificate that
expires while the process is up means an outage.

HOW. uvicorn builds one `ssl.SSLContext` and every accepted connection uses it.
Python lets a server context carry an `sni_callback` that runs during each
handshake and may replace the connection's context. This installs one that
always points at the *current* context. Rotation is then: build a complete new
context off to the side, and if (and only if) that succeeds, swap the pointer.
It changes both the certificate the server presents and the CA it trusts for
client certificates, and it works for clients that send no SNI (a connection by
IP address): the callback fires with `server_name=None`. The tests prove each.

SAFETY, in the same spirit as a policy bundle reload (invariant 3):

* A bad rotation never empties trust or drops the listener. A half-written
  file, a key that does not match its certificate, or an empty CA file makes
  the *new* context fail to build, and the previous one keeps serving.
* The new context is validated as a whole before it is used; there is no window
  where it holds the new server certificate but the old CA.
* Startup is the opposite: unusable material at boot raises (uvicorn cannot
  build its context), because a server that came up with no client
  verification would look identical to one that worked.

WHAT THIS IS NOT. Rotation is not revocation. A connection that completed its
handshake before a swap keeps the identity it authenticated with until it
closes. Removing a CA stops *new* handshakes from that CA immediately; it does
not tear down connections already open. Bound that with short certificate
lifetimes and short keep-alives, not with this.
"""

from __future__ import annotations

import hashlib
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TlsFiles:
    cert: Path
    key: Path | None  # None when the certificate file also holds the private key
    client_ca: Path
    client_auth: str  # "required" | "optional"

    def paths(self) -> tuple[Path, ...]:
        return tuple(p for p in (self.cert, self.key, self.client_ca) if p is not None)


_VERIFY_MODES = {"required": ssl.CERT_REQUIRED, "optional": ssl.CERT_OPTIONAL}


def build_context(files: TlsFiles) -> ssl.SSLContext:
    """A complete server context, or an exception. Mirrors exactly what uvicorn
    builds from the same settings, so a reloaded context behaves like the
    original. Raises `ssl.SSLError` / `OSError` / `ValueError`."""
    if files.client_auth not in _VERIFY_MODES:
        raise ValueError(f"client_auth must be 'required' or 'optional', not {files.client_auth!r}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(files.cert), str(files.key) if files.key else None)
    ctx.verify_mode = _VERIFY_MODES[files.client_auth]
    ctx.load_verify_locations(str(files.client_ca))
    return ctx


def _digest(files: TlsFiles) -> str | None:
    """A fingerprint of every file's current content, or None if any cannot be
    read right now (a rotation in progress). Content, not mtime: a CSI secrets
    mount and a Kubernetes Secret volume swap a `..data` symlink, which
    mtime-based checks miss."""
    h = hashlib.sha256()
    for path in files.paths():
        try:
            data = path.read_bytes()
        except OSError:
            return None
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


class TlsReloader:
    def __init__(
        self,
        files: TlsFiles,
        *,
        interval_s: float = 30.0,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._files = files
        self._interval_s = interval_s
        # Called with ("tls_reloaded" | "tls_reload_failed", details) so a caller
        # can surface rotations (the control plane shows them). A callback that
        # raises must never affect what the gateway serves.
        self._on_event = on_event
        # Taken BEFORE uvicorn builds its context (see serve.build_server): a
        # rotation that lands between the two would otherwise be recorded as
        # already-loaded and never applied.
        self._digest = _digest(files)
        self._failed_digest: str | None = None
        self._current: ssl.SSLContext | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.generation = 0
        self.last_error: str | None = None
        self.last_reload_at: float | None = None

    def _emit(self, event_type: str, **details: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, details)
        except Exception:
            log.exception("tls_event_callback_failed")

    def attach(self, base: ssl.SSLContext) -> None:
        """Install the swap hook on the context uvicorn built."""
        self._current = base
        base.sni_callback = self._swap

    def _swap(self, sslobj: Any, server_name: str | None, initial_ctx: ssl.SSLContext) -> None:
        # Runs on the event loop for every handshake; must not raise and must
        # not block. A plain reference read.
        current = self._current
        if current is not None:
            sslobj.context = current
        return None

    def check(self) -> bool:
        """Reload if the material changed. True if a new context is now live."""
        with self._lock:
            digest = _digest(self._files)
            if digest is None or digest == self._digest or digest == self._failed_digest:
                # Unreadable (mid-rotation), unchanged, or already known bad:
                # nothing to do. A bad file that is later fixed changes the
                # digest, so it is retried without polling the same failure.
                return False
            try:
                fresh = build_context(self._files)
            except (ssl.SSLError, OSError, ValueError) as exc:
                self._failed_digest = digest
                self.last_error = str(exc)
                log.error(
                    "tls_reload_failed_keeping_previous",
                    error=str(exc),
                    generation=self.generation,
                )
                self._emit("tls_reload_failed", error=str(exc), generation=self.generation)
                return False
            self._current = fresh
            self._digest = digest
            self._failed_digest = None
            self.last_error = None
            self.generation += 1
            self.last_reload_at = time.time()
            log.info("tls_reloaded", generation=self.generation)
            self._emit("tls_reloaded", generation=self.generation)
            return True

    def start(self) -> None:
        if self._interval_s <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="tls-reload", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.check()
            except Exception:  # never let the watcher die silently
                log.exception("tls_reload_watcher_error")
