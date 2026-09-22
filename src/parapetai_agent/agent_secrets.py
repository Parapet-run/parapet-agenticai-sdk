"""The bearer-secret scheme shared by everything that mints or checks an
agent_id/secret pair: the control plane's own provisioning
(parapetai_control.bundles.provision_agent / parapetai_control.auth, consumed
via PyPI) and a gateway's local shared-secret identity bindings
(gateway/src/parapetai_gateway/identity/bindings.py, consumed in-workspace).

Same shape as signing.py's own history: this used to exist only inside the
control plane's private repo. A gateway binding that stores a copy of the
same hash needs to compute it identically, or a secret that is valid to the
control plane would be silently rejected (or, worse, a difference in the
algorithm could make an invalid secret verify) at the gateway. Single-sourcing
it here makes drift impossible by construction instead of relying on two
independently-maintained implementations staying accidentally in sync.

Scaffold-grade on purpose, matching the control plane's own original
docstring: no KMS, no pepper, no rotation schedule. The property that matters
holds regardless -- `generate_secret()` returns a `secrets.token_urlsafe(32)`
value (256 bits from `secrets`, not a user-chosen password), so a stolen hash
is not practically reversible the way a password hash would be.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_secret() -> str:
    """A fresh, high-entropy bearer secret. Never derived from anything the
    caller supplies -- an operator-chosen value would be guessable/reused."""
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """The value actually stored (never the raw secret). Deterministic, so
    the same secret always hashes the same way on both sides of a lookup."""
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_matches(secret: str, expected_hash: str) -> bool:
    """Constant-time comparison against a stored hash -- a per-character
    timing difference must not leak how much of a guess was correct."""
    return hmac.compare_digest(hash_secret(secret), expected_hash)
