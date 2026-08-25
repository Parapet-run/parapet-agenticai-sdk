"""Caller identity resolution.

Identity comes from a base-URL path prefix (/a/{agent_id}/...), not from a
gateway-held token or a control-plane lookup. This is what lets per-agent
policy and audit attribution work with zero token distribution -- an adopter
just embeds its own id in the base URL it was already going to set.

UNAUTHENTICATED, THEREFORE SPOOFABLE: agent_id is caller-supplied and never
verified against any credential. Any request naming an agent_id is trusted as
that agent. This is acceptable only where the gateway's whole reachable
surface is pod-local trust (the same boundary ADR 0002's NetworkPolicy egress
backstop already assumes) -- it is not a substitute for real authentication in
a multi-tenant deployment, where a hostile neighbour could claim any agent_id.
See docs/adr/0003-path-prefix-identity-and-credential-passthrough.md.

Requests with no prefix resolve to Agent::"anonymous". That is still a real
principal evaluated by Cedar's default-deny policy set -- never a bypass.
"""

from __future__ import annotations

from dataclasses import dataclass

_PREFIX = "/a/"
ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class Caller:
    agent_id: str
    tenant: str = "default"
    trust_tier: str = "standard"

    @property
    def principal(self) -> str:
        return f'Agent::"{self.agent_id}"'


def resolve_from_path(path: str) -> tuple[Caller, str]:
    """Strip a leading /a/{agent_id} segment, returning (caller, remaining path).

    An empty agent_id segment (bare "/a/" or "/a") does not count as a claim --
    falls through to anonymous with the path untouched, same as no prefix at
    all.
    """
    if path.startswith(_PREFIX):
        rest = path[len(_PREFIX) :]
        agent_id, _, remainder = rest.partition("/")
        if agent_id:
            return Caller(agent_id=agent_id), "/" + remainder
    return Caller(agent_id=ANONYMOUS), path
