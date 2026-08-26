"""Client fingerprint derivation from request headers.

Headers only -- never request/response bodies. A table, not an if-chain:
adding a client is one line in _SHAPES, not a new branch. An unrecognised
user-agent is recorded raw with client_name="unknown" rather than dropped;
that a request didn't match anything known is itself the signal worth seeing.

Known shapes below were captured empirically (a local header-echo server, not
vendor docs) from each SDK's real outbound request. Two findings that would
have been wrong if assumed instead of verified:

    - The async client variant changes the UA prefix: openai-python's
      AsyncOpenAI sends "AsyncOpenAI/Python <ver>", not "OpenAI/Python <ver>"
      (anthropic-python: same pattern, AsyncAnthropic). Same package, so both
      prefixes map to the same client_name.
    - openai-agents sets its OWN UA ("Agents/Python <ver>", its own package
      version) on top of an internally-constructed AsyncOpenAI client -- but
      that inner client still stamps x-stainless-package-version with the
      *openai* package's version, not agents'. Using that header here would
      silently record the wrong version, so this shape has no version_header
      and parses its own version from the UA instead.

    openai-python    UA "(Async)?OpenAI/Python 2.52.0"; x-stainless-package-version
    openai-agents    UA "Agents/Python 0.19.2"; x-stainless-package-version present
                     but belongs to openai, not agents -- do not use it here.
    anthropic-python UA "(Async)?Anthropic/Python 0.120.2"; x-stainless-package-version
    google-genai     UA "google-genai-sdk/2.16.0 gl-python/3.12.13"; no x-stainless
    litellm          UA "litellm/<version>" -- but only on litellm's own
                     generic HTTP path. When litellm delegates to a vendor
                     SDK (e.g. openai-python for an "openai/..." model), the
                     wire traffic carries that SDK's UA, not litellm's. That
                     is correct here: this fingerprints what is actually on
                     the wire, not which library the caller thinks it used.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClientShape:
    name: str
    ua_pattern: re.Pattern[str]  # must define a "version" group to be used as a fallback
    version_header: str | None = None  # takes priority over the UA-parsed version when present


_SHAPES: tuple[ClientShape, ...] = (
    ClientShape(
        "openai-python",
        re.compile(r"^(?:Async)?OpenAI/Python (?P<version>\S+)"),
        "x-stainless-package-version",
    ),
    ClientShape("openai-agents", re.compile(r"^Agents/Python (?P<version>\S+)")),
    ClientShape(
        "anthropic-python",
        re.compile(r"^(?:Async)?Anthropic/Python (?P<version>\S+)"),
        "x-stainless-package-version",
    ),
    ClientShape("google-genai", re.compile(r"^google-genai-sdk/(?P<version>\S+)")),
    ClientShape("litellm", re.compile(r"^litellm/(?P<version>\S+)", re.IGNORECASE)),
)


def fingerprint(headers: Mapping[str, str]) -> tuple[str, str | None]:
    """Derive (client_name, client_version) from user-agent and x-stainless-* headers.

    Unmatched: (UNKNOWN, raw user-agent) -- recorded, not discarded.
    """
    user_agent = headers.get("user-agent", "")
    for shape in _SHAPES:
        match = shape.ua_pattern.match(user_agent)
        if not match:
            continue
        version = headers.get(shape.version_header) if shape.version_header else None
        if not version:
            version = match.groupdict().get("version")
        return shape.name, version
    return UNKNOWN, user_agent or None
