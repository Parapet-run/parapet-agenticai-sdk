"""Mints a demo JWT for adk_webapp/'s curl examples.

NOT a real, signed token -- this repo has no IdP to issue one from.
`parapetai_agent.token_identity.decode_jwt_claims()` never verifies a
signature (see that module's own docstring, "Signature verification"
section, for exactly what it does and doesn't prove and when that's an
appropriate tradeoff), so a structurally-valid, UNSIGNED JWT (three
dot-separated segments, a base64url JSON payload -- the signature segment
left empty) is exactly as good a fixture for demonstrating what
IdentityMiddleware/jwt_bearer_extractor() do with whatever arrives on the
Authorization header as a real one would be. Appropriate ONLY because
nothing here is meant to actually rely on this as proof of who's calling
-- see token_identity.py's own docstring for when this tradeoff is and
isn't appropriate for a real deployment (short version: only if something
upstream, e.g. a real gateway/TLS-terminated auth, already validated the
token before this process ever saw it).

Usage:
    python3 mint_demo_jwt.py --sub alice --roles OrderViewer
    python3 mint_demo_jwt.py --sub bob --roles Guest
    python3 mint_demo_jwt.py --sub carol   # no roles at all
"""

from __future__ import annotations

import argparse
import base64
import json


def _b64url(payload: dict[str, object]) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint(*, sub: str, roles: list[str]) -> str:
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url({"sub": sub, "oid": sub, "roles": roles})
    return f"{header}.{payload}."  # empty signature segment -- never checked, see module docstring


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sub", default="demo-user", help="subject/oid claim (also used as-is)")
    parser.add_argument(
        "--roles", nargs="*", default=[], help="role claims, e.g. --roles OrderViewer"
    )
    args = parser.parse_args()

    token = mint(sub=args.sub, roles=args.roles)
    print(token)


if __name__ == "__main__":
    main()
