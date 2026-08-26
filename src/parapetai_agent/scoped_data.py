"""Framework-agnostic identity primitives: end-user identity and agent
identity, ambient (contextvars-backed) or explicit, shared by every
in-process framework integration this package ships (parapetai_agent.maf,
parapetai_agent.adk, ...).

Originally lived inside parapetai_agent/maf.py, module-private, even though
nothing about it depends on agent_framework -- it's pure contextvars plus
parapetai_agent.identity.Caller/parapetai_agent.token_identity, both already
framework-agnostic. Extracted here so a second framework integration
(parapetai_agent/adk.py) can import the exact same identity API rather than
reimplementing it: an agent author who has governed_identity()/
identity_from_bearer_token() calls in their app and switches which
GovernedAgent/GovernedRunner they construct changes zero identity code.

Two identities, not one -- which is which matters for telemetry, same
distinction maf.py's own module docstring documents in detail:

  ScopedData.end_user (Identity: claims + roles) is the END USER's identity
  -- e.g. Bob, verified via a real token -- and CAN change every call.

  ScopedData.agent (AgentIdentity: claims) is the calling AGENT's own
  identity -- a Service Principal/client_id, when available from a token --
  and overrides the static agent_id string a Caller/GovernedAgent(...) or
  GovernedRunner/build_plugin(...) was otherwise constructed with, for as
  long as it's set. See _effective_principal().

Nothing here validates a token -- it trusts whatever the caller already
verified and passes through as Cedar context attributes (Snapshot.
identity_claims / Snapshot.identity_roles, parapetai_agent/providers/
parsers.py).
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from parapetai_agent.identity import Caller
from parapetai_agent.token_identity import (
    ExtractedIdentity,
    JwtIdentityExtractor,
    TokenIdentityExtractor,
)


@dataclass(slots=True, frozen=True)
class Identity:
    """The end user's identity -- what set_current_identity()/
    current_identity() puts in _current_identity. Identity has no ambient
    source in any framework's own context objects (verified directly
    against agent_framework's AgentSession, which carries only
    session_id/service_session_id, nothing identity-shaped) -- it has to
    enter from the embedding application at least once."""

    claims: dict[str, str] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class AgentIdentity:
    """The calling agent's own identity -- what set_current_agent_identity()/
    agent_identity() puts in _current_agent_identity. Claims here typically
    come from parapetai_agent.token_identity.agent_identity_from_claims() (an
    RFC 8693 `act` claim, or an azp/appid fallback), but nothing here
    requires that specific source."""

    claims: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ScopedData:
    """One container for both halves of governed identity -- the shape a
    framework adapter's own callback/hook can read in one place when
    building a Snapshot. Not itself stored in a ContextVar; a convenience
    view over current_identity()'s/agent_identity()'s ambient state, built
    on demand via current_scoped_data()."""

    end_user: Identity
    agent: AgentIdentity


_current_identity: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "parapetai_agent_scoped_data_current_identity", default=None
)


def set_current_identity(
    *, claims: Mapping[str, Any] | None = None, roles: Sequence[Any] | None = None
) -> contextvars.Token[Identity | None]:
    """Sets the end user's identity ambiently for every governed
    model_call/tool_call decision made from here on, in this asyncio task
    (and any child task created from within it -- ordinary contextvars
    copy-on-task-creation semantics), without repeating identity_claims/
    identity_roles on every single call.

    Returns a contextvars.Token -- pass it to reset_current_identity() to
    restore whatever was set before (typically None). Prefer the
    current_identity() context manager below for the common case of "set
    for the duration of one block"; use set_current_identity()/
    reset_current_identity() directly only when the set and the reset
    happen in genuinely different places (e.g. framework request/response
    hooks that don't share a single `with` block).

    An explicit identity_claims/identity_roles passed per-call by a
    framework adapter still takes precedence over whatever is set here --
    see effective_identity_claims()/effective_identity_roles()."""
    identity = Identity(
        claims={str(k): str(v) for k, v in (claims or {}).items()},
        roles=[str(r) for r in (roles or [])],
    )
    return _current_identity.set(identity)


def reset_current_identity(token: contextvars.Token[Identity | None]) -> None:
    """Pairs with set_current_identity() -- see its docstring."""
    _current_identity.reset(token)


@contextlib.contextmanager
def current_identity(
    *, claims: Mapping[str, Any] | None = None, roles: Sequence[Any] | None = None
) -> Iterator[None]:
    """`with current_identity(claims=..., roles=...):` -- sets the end
    user's identity ambiently for every governed decision made anywhere
    inside this block (including everything it awaits), then restores the
    previous value on exit, even if the block raises. See
    set_current_identity()'s docstring for why this exists and how
    precedence against an explicit per-call identity works."""
    token = set_current_identity(claims=claims, roles=roles)
    try:
        yield
    finally:
        reset_current_identity(token)


_current_agent_identity: contextvars.ContextVar[AgentIdentity | None] = contextvars.ContextVar(
    "parapetai_agent_scoped_data_current_agent_identity", default=None
)


def set_current_agent_identity(
    *, claims: Mapping[str, Any] | None = None
) -> contextvars.Token[AgentIdentity | None]:
    """Sets the AGENT's own identity ambiently (e.g. a Service Principal's
    client_id, decoded from a token's RFC 8693 act claim or azp/appid --
    see parapetai_agent.token_identity), overriding the static agent_id a
    Caller/GovernedAgent/GovernedRunner was constructed with for as long as
    it's set -- see effective_principal(). Same set()/reset(token) shape as
    set_current_identity(); see that function's docstring for the general
    rationale (ambient, contextvars-backed, correctly isolated per asyncio
    task)."""
    return _current_agent_identity.set(
        AgentIdentity(claims={str(k): str(v) for k, v in (claims or {}).items()})
    )


def reset_current_agent_identity(token: contextvars.Token[AgentIdentity | None]) -> None:
    """Pairs with set_current_agent_identity() -- see its docstring."""
    _current_agent_identity.reset(token)


@contextlib.contextmanager
def agent_identity(*, claims: Mapping[str, Any] | None = None) -> Iterator[None]:
    """`with agent_identity(claims={"client_id": "..."}):` -- the
    context-manager form of set_current_agent_identity()/
    reset_current_agent_identity(), for the common case of "set for the
    duration of one block". Usually reached via identity_from_bearer_token()
    rather than called directly."""
    token = set_current_agent_identity(claims=claims)
    try:
        yield
    finally:
        reset_current_agent_identity(token)


def effective_principal(caller: Caller) -> str:
    """Cedar's principal for this decision. Ambient agent identity (set via
    agent_identity()/identity_from_bearer_token(), typically from a real
    token's act/azp/appid claim -- a genuine Service Principal identity)
    takes precedence over caller.principal (the static agent_id string a
    Caller/GovernedAgent/GovernedRunner was constructed with) when present;
    otherwise falls back to caller.principal exactly as before this
    existed. This is what makes agent_id=... optional in practice once a
    real identity is available from a token, without requiring it be
    chosen upfront."""
    agent = _current_agent_identity.get()
    if agent and agent.claims:
        identifier = (
            agent.claims.get("client_id")
            or agent.claims.get("oid")
            or agent.claims.get("sub")
            or "unknown"
        )
        return f'Agent::"{identifier}"'
    return caller.principal


def identity_from_bearer_token(
    token: str, *, extractor: TokenIdentityExtractor | None = None
) -> _CombinedIdentityContext:
    """Decodes ONE bearer token into BOTH end-user and agent identity, and
    sets both ambiently for the duration of a `with` block:

        with identity_from_bearer_token(token):
            await agent.run(...)   # or: async for event in runner.run_async(...)

    extractor defaults to token_identity.JwtIdentityExtractor(); pass a
    different TokenIdentityExtractor (see that Protocol's docstring) to
    support a non-JWT token format. Either half of the extracted identity
    may come back empty -- that's not an error, see ExtractedIdentity's
    docstring -- and this still sets both context managers regardless, so
    "asserted but empty" (a real user with zero roles, or a token with no
    delegation) is preserved correctly rather than collapsed into "nothing
    was asserted at all" (see Snapshot.to_context()'s own docstring for why
    that distinction matters to Cedar's `has` checks)."""
    identity = (extractor or JwtIdentityExtractor()).extract(token)
    return _CombinedIdentityContext(identity)


@contextlib.contextmanager
def governed_identity(
    *,
    claims: Mapping[str, Any] | None = None,
    roles: Sequence[Any] | None = None,
    token: str | None = None,
    extractor: TokenIdentityExtractor | None = None,
) -> Iterator[None]:
    """ONE context manager for every identity source this module knows how
    to read directly -- pick exactly one of (claims and/or roles) or token,
    and the underlying mechanism (current_identity() /
    identity_from_bearer_token()) is chosen for you:

        # claims/roles already parsed
        with governed_identity(claims={"oid": "..."}, roles=["OrderViewer"]):
            await agent.run(query)

        # a raw bearer token
        with governed_identity(token=jwt):
            await agent.run(query)

    A framework-specific credential source (e.g. maf.identity_from_azure_
    credential(), for azure-identity credentials) builds on
    identity_from_bearer_token() the same way but lives in its own
    framework module, not here, since it needs that framework's own
    optional dependency.

    Fails LOUD, not silent, on ambiguity -- unlike an unwrapped call, which
    evaluates against EMPTY identity_claims/identity_roles (Cedar's own
    default-deny already makes that the safe failure mode for any policy
    that checks identity: denied, not skipped or bypassed), a
    MISCONFIGURED call to this function raises immediately instead of
    quietly doing the wrong thing:
      - zero sources given: ValueError -- if there's genuinely no identity
        to assert, call the framework's own run method directly, unwrapped,
        rather than this function with nothing in it.
      - more than one source given: ValueError -- an author who passed both
        claims/roles and token= almost certainly left one in by mistake
        while editing, not intended "combine both somehow"; there's no
        defined merge semantics across two different sources to fall back
        on either.
    """
    has_claims_or_roles = claims is not None or roles is not None
    sources_given = sum([has_claims_or_roles, token is not None])
    if sources_given == 0:
        raise ValueError(
            "governed_identity() needs exactly one identity source (claims/roles or token) -- "
            "call the framework's own run method directly, unwrapped, if there's genuinely no "
            "identity to assert for this call"
        )
    if sources_given > 1:
        raise ValueError(
            "governed_identity() got more than one identity source -- pass exactly one of "
            "claims/roles or token, not a combination"
        )

    if token is not None:
        with identity_from_bearer_token(token, extractor=extractor):
            yield
    else:
        with current_identity(claims=claims, roles=roles):
            yield


class _CombinedIdentityContext:
    """Backs identity_from_bearer_token()'s `with` block -- a plain class,
    not @contextlib.contextmanager, because it needs to enter/exit TWO
    independent context managers (end-user + agent identity) together and
    still restore each correctly even if only one was ever set."""

    def __init__(self, identity: ExtractedIdentity) -> None:
        self._identity = identity
        self._user_token: contextvars.Token[Identity | None] | None = None
        self._agent_token: contextvars.Token[AgentIdentity | None] | None = None

    def __enter__(self) -> None:
        self._user_token = set_current_identity(
            claims=self._identity.end_user_claims, roles=self._identity.end_user_roles
        )
        if self._identity.agent_claims:
            self._agent_token = set_current_agent_identity(claims=self._identity.agent_claims)

    def __exit__(self, *exc_info: object) -> None:
        if self._user_token is not None:
            reset_current_identity(self._user_token)
        if self._agent_token is not None:
            reset_current_agent_identity(self._agent_token)


def effective_identity_claims(explicit: Mapping[str, Any] | None) -> dict[str, str]:
    """Explicit per-call identity claims win if present; otherwise falls
    back to whatever set_current_identity()/current_identity() set
    ambiently for the current asyncio task. Falling back, not merging: an
    explicit per-call value fully replaces the ambient one (matches how
    you'd expect an explicit override to behave), it doesn't get combined
    with it field-by-field."""
    if explicit is not None:
        return {str(k): str(v) for k, v in explicit.items()}
    ambient = _current_identity.get()
    return dict(ambient.claims) if ambient else {}


def effective_identity_roles(explicit: Sequence[Any] | None) -> list[str]:
    """A role claim (e.g. Entra ID app roles) is a SET, not a scalar --
    kept separate from effective_identity_claims for the same reason
    Snapshot.identity_roles is its own field, not folded into
    identity_claims. See that field's docstring. Same
    explicit-wins-ambient-fallback precedence as effective_identity_claims."""
    if explicit is not None:
        return [str(r) for r in explicit]
    ambient = _current_identity.get()
    return list(ambient.roles) if ambient else []


def current_scoped_data() -> ScopedData:
    """A read-only snapshot of whatever end-user/agent identity is
    currently ambient, for a framework adapter that wants ScopedData's
    combined shape directly rather than reading each ContextVar itself."""
    identity = _current_identity.get() or Identity()
    agent = _current_agent_identity.get() or AgentIdentity()
    return ScopedData(end_user=identity, agent=agent)
