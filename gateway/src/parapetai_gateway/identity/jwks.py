"""Where a JWT's verification key comes from.

Keys come ONLY from the identity provider's published key set (or, in tests
and offline deployments, a fixed map) -- never from anything in the token
itself. The token's `kid` selects among keys the operator already trusts; it
cannot introduce one.

The network fetch is the one part of identity verification that can be abused
or can fail, so it is built defensively:

* HTTPS only. A key set fetched over plain HTTP can be swapped in transit.
* Refetches are rate-limited (`min_refresh_s`). Without that, an attacker
  could send tokens with random `kid`s and turn the gateway into an outbound
  request amplifier against the IdP -- and a refetch under lock stalls every
  other verification behind it.
* An unreachable IdP does not open anything. Previously fetched keys keep
  verifying tokens for up to `max_stale_s` (so a brief IdP outage doesn't take
  every agent down), after which they are dropped and verification fails
  closed. A key the gateway has never seen is never accepted on the strength
  of an outage.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx
import jwt
import structlog

log = structlog.get_logger(__name__)


class KeyProvider(Protocol):
    def get_key(self, kid: str) -> Any | None:
        """The verification key for `kid`, or None if it is not (or no longer)
        trusted. Never raises for an unknown key: unknown means untrusted."""
        ...


class StaticKeyProvider:
    """A fixed `kid -> key` map: tests, and deployments that pin keys."""

    def __init__(self, keys: Mapping[str, Any]) -> None:
        self._keys = dict(keys)

    def get_key(self, kid: str) -> Any | None:
        return self._keys.get(kid)


class JwksKeyProvider:
    """Fetches and caches a JWKS document (RFC 7517) over HTTPS."""

    def __init__(
        self,
        url: str,
        *,
        ttl_s: float = 3600.0,
        max_stale_s: float = 86400.0,
        min_refresh_s: float = 30.0,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        client: httpx.Client | None = None,
    ) -> None:
        if not url.lower().startswith("https://"):
            raise ValueError(f"JWKS URL must be https:// (got {url!r})")
        self._url = url
        self._ttl_s = ttl_s
        self._max_stale_s = max_stale_s
        self._min_refresh_s = min_refresh_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._client = client
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None  # last SUCCESSFUL fetch
        self._attempted_at: float | None = None  # last attempt, success or not

    def get_key(self, kid: str) -> Any | None:
        with self._lock:
            now = self._clock()
            if self._needs_refresh(kid, now):
                self._refresh(now)
            if self._fetched_at is None or now - self._fetched_at > self._max_stale_s:
                # Never fetched, or the IdP has been unreachable for longer
                # than we are willing to trust what we last saw.
                self._keys = {}
                return None
            return self._keys.get(kid)

    def _needs_refresh(self, kid: str, now: float) -> bool:
        if self._attempted_at is not None and now - self._attempted_at < self._min_refresh_s:
            return False  # rate limit: applies to a miss AND to a stale cache
        if self._fetched_at is None:
            return True
        if now - self._fetched_at > self._ttl_s:
            return True
        return kid not in self._keys  # possible rotation

    def _refresh(self, now: float) -> None:
        self._attempted_at = now
        try:
            if self._client is not None:
                response = self._client.get(self._url, timeout=self._timeout_s)
            else:
                with httpx.Client(follow_redirects=False) as client:
                    response = client.get(self._url, timeout=self._timeout_s)
            response.raise_for_status()
            keys = _parse_jwks(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            # Keep whatever we had; get_key() decides whether it is still
            # young enough to trust.
            log.warning("jwks_fetch_failed", url=self._url, error=str(exc))
            return
        self._keys = keys
        self._fetched_at = now


def _parse_jwks(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("JWKS document has no `keys` array")
    keys: dict[str, Any] = {}
    for jwk in document["keys"]:
        if not isinstance(jwk, dict) or not isinstance(jwk.get("kid"), str):
            continue
        if jwk.get("use") not in (None, "sig"):
            continue  # an encryption key is not a signing key
        try:
            keys[jwk["kid"]] = jwt.PyJWK.from_dict(jwk).key
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            # One malformed key must not discard the rest of the set.
            log.warning("jwks_key_skipped", kid=jwk["kid"], error=str(exc))
    return keys
