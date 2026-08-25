"""Ed25519 identity for a PEP process -- signs requests to the control
plane (parapetai_agent.control_plane's fetch_bundle/send_heartbeat) so it can
verify they actually came from whoever holds this process's private key,
on top of (not instead of) the bearer secret sent on every request today.
See control-plane/src/parapetai_control/keys.py for the verification side and the
full reasoning for layering this on top of the bearer secret rather than
replacing it. signing_payload (parapetai_agent.signing) is the one shared
definition both sides sign/verify against.

The private key is generated once and persisted to disk (default
~/.parapetai/pep_ed25519.key, override via PARAPETAI_PEP_KEY_PATH) -- a PEP's
identity should be stable across restarts, not reminted every time: each
new key needs a fresh registration call, and every restart would put the
control plane's "current" key one rotation further from whatever the
handful of requests right before that restart were actually signed with
(still fine -- verify_signature checks the previous key too -- but there's
no reason to churn it on every restart when nothing about the PEP's
identity actually changed).

Written with mode 0600: a private key readable by other local users
defeats the entire point of this being a secret that never leaves the
process. Not encrypted at rest -- same trust model this repo already
applies to the bearer secret file/env var itself; a host that can read
either was already inside the PEP's own trust boundary.
"""

from __future__ import annotations

import base64
import os
import stat
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from parapetai_agent.signing import signing_payload


def default_key_path() -> Path:
    return Path(
        os.environ.get(
            "PARAPETAI_PEP_KEY_PATH", str(Path.home() / ".parapetai" / "pep_ed25519.key")
        )
    )


def generate_ephemeral_keypair() -> Ed25519PrivateKey:
    """A fresh keypair, never touching disk -- for a process with no
    writable filesystem at all (a read-only container root, a
    serverless function with no mounted volume). Trades this PEP's
    identity STABILITY across restarts for that: a new keypair every
    cold start means a fresh registration call every cold start (see
    parapetai_agent.control_plane.ensure_pep_identity's persist= param) --
    not broken, since verify_signature already checks the previous key
    too and a control-plane-initiated rotation is a no-op here (nothing
    to rotate TO that would survive the next restart anyway, see
    parapetai_agent.control_plane.run_bundle_poller's key_path=None handling),
    just less efficient than a key that's actually stable across
    restarts. Prefer load_or_create_keypair() whenever ANY writable path
    is available, even an ephemeral one that resets between cold starts
    (e.g. AWS Lambda's /tmp) -- that still gets you a stable identity for
    the lifetime of one warm container, this gets you a new one on every
    single invocation of a fully stateless function."""
    return Ed25519PrivateKey.generate()


def load_or_create_keypair(key_path: Path | None = None) -> Ed25519PrivateKey:
    """Loads the raw 32-byte private key at key_path (default
    default_key_path()), generating and persisting a new one on first use.
    Not thread-safe against a concurrent first-use race across multiple
    processes sharing the same key_path -- acceptable here: this repo's
    own scaffold-grade stance already accepts the same non-issue for
    parapetai_control.db's single SQLite file, and the race's worst case is a second
    process overwriting the file with ANOTHER valid keypair, not
    corruption -- the loser just re-registers with the control plane on
    its next startup."""
    path = key_path or default_key_path()
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    private_key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private_key.private_bytes_raw())
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return private_key


def rotate_keypair(key_path: Path | None = None) -> Ed25519PrivateKey:
    """Unlike load_or_create_keypair, ALWAYS generates a fresh keypair and
    overwrites whatever was at key_path -- used when an operator has
    asked (via the control plane's "trigger rotation" action,
    parapetai_control/agent_auth.py) for this PEP to stop signing with its current
    key. The caller is responsible for registering the new public key
    afterward (parapetai_agent.control_plane.register_public_key) -- this
    function only touches the local file."""
    path = key_path or default_key_path()
    private_key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private_key.private_bytes_raw())
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return private_key


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def sign_headers(
    private_key: Ed25519PrivateKey, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    """The two headers a signed request needs: X-Parapetai-Signature (base64)
    and X-Parapetai-Signed-At (the exact string signed over -- the control
    plane verifies against this literal string, not a re-parsed float, so
    it must be sent exactly as generated here)."""
    signed_at = str(time.time())
    payload = signing_payload(method, path, signed_at, body)
    signature = base64.b64encode(private_key.sign(payload)).decode()
    return {"X-Parapetai-Signature": signature, "X-Parapetai-Signed-At": signed_at}
