"""The Ed25519 signing contract shared by a PEP (parapetai_agent.pep_identity,
consumed by parapetai_agent and gateway/src/parapetai_gateway/server/main.py) and the
control plane that verifies its signatures (control-plane/src/parapetai_control/keys.py).

Previously this was two byte-identical copies -- one on each side of the
package boundary -- provable equal only via a cross-package test
(deliberately, at the time: parapetai_control had no runtime dependency on the gateway
package). Now that both sides can depend on a genuinely shared,
open-source foundation, there is exactly one implementation; drift is
impossible by construction instead of proven absent by a test.
"""

from __future__ import annotations


def signing_payload(method: str, path: str, signed_at: str, body: bytes) -> bytes:
    """The exact bytes both a PEP and a control plane sign/verify --
    method + path + the literal X-Parapetai-Signed-At header STRING (not a
    re-parsed float; formatting the same timestamp differently on either
    side would otherwise produce a payload mismatch neither side did
    anything wrong to cause) + the raw request body (empty for a bodyless
    GET). path is expected to be the request path only, no query string --
    keeps both sides' encoding of query params from having to match
    byte-for-byte, and nothing in this protocol puts anything
    security-relevant in one."""
    return f"{method.upper()}\n{path}\n{signed_at}\n".encode() + body
