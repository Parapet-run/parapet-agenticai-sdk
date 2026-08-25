"""Persists an identity across separate calls under a developer-chosen
key, so it doesn't have to be re-derived (or re-threaded through by hand)
every time -- the piece `current_identity()`/`IdentityMiddleware` don't
provide on their own, since both require the caller to already have
`(claims, roles)` in hand at the point of use.

Pure stdlib at module level -- no Starlette import, so this is usable
without the `web` extra (a CLI script, a batch worker), matching the
existing core/`web`-extra split `identity_middleware.py` already
established. `parapetai_agent.maf.current_identity` is imported lazily,
inside use_identity() below, not at module level: this module is one of
the ones `parapetai_agent/__init__.py` imports unconditionally (control-
plane and gateway reach it indirectly just by importing any submodule of
this package, e.g. `parapetai_agent.signing`), so a module-level import
here would make `agent_framework` a hard dependency of the gateway and
control plane too -- exactly what CLAUDE.md's "never a runtime dependency
of the core gateway" rule forbids. Only actually CALLING use_identity()
needs `agent_framework` present, which is a real and correct constraint
(this function's whole job is layering onto MAF's contextvar).

## The pattern this replaces

`examples/maf_webapp/web_app.py` hand-rolls exactly this today -- a
`sessions: dict[str, dict]` keyed by a cookie. That's fine for one
example, but it's copy-pasteable, in-memory-only, and entangled with that
file's own unrelated conversation-history/model-choice state, not a
reusable primitive. This module is that primitive, extracted.

## CLI / non-interactive usage -- the actual gap this closes

`IdentityMiddleware` is Starlette-only by design: it wraps one HTTP
request. A CLI script, or a batch process handling several distinct
invokers in one run, has no request to wrap. The pattern there is instead:

    set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])
    ...
    with use_identity("alice"):
        await agent.run(...)

`set_identity()` once (e.g. right after that invoker's own login/lookup
step), then `use_identity(key)` around each unit of work for that
invoker -- see `examples/maf_cli/run_example.py` for a real, runnable
version of this with several distinct invokers in one process.

`current_identity()`/`set_current_identity()` (parapetai_agent.maf, unchanged)
stay the right tool for a caller with exactly one identity for its whole
process lifetime and no reason to name a key at all.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping, Sequence
from enum import Enum
from typing import Any, Protocol


class IdentityKeyKind(Enum):
    """Namespaces a key so two unrelated concepts that happen to share a
    string value never collide in the store -- e.g. an app with both a
    `session_id` and an unrelated `thread_id` concept, where nothing
    guarantees those value spaces don't overlap. Purely a namespacing tag;
    it doesn't change how long a key's identity persists -- that's a
    property of the configured IdentityStore (see InMemoryIdentityStore's
    docstring), not of which kind a key is."""

    SESSION = "session"
    THREAD = "thread"
    CUSTOM = "custom"


class IdentityStore(Protocol):
    """Pluggable storage for set_identity()/get_identity()/use_identity()
    below -- implement this to back the store with something that outlives
    one process (Redis, a database) instead of the in-memory default. A
    Protocol, not a base class to subclass, matching
    token_identity.TokenIdentityExtractor's existing precedent in this
    package: any object with matching get/set/clear methods works."""

    def get(self, namespaced_key: str) -> tuple[Mapping[str, Any], Sequence[str]] | None: ...

    def set(
        self, namespaced_key: str, *, claims: Mapping[str, Any], roles: Sequence[Any]
    ) -> None: ...

    def clear(self, namespaced_key: str) -> None: ...


class InMemoryIdentityStore:
    """Default store -- a plain dict guarded by a threading.Lock. The lock
    is not defensive-for-its-own-sake: real concurrent writers already
    exist in this repo (examples/maf_webapp/web_app.py's /login route runs
    off the event loop via starlette.concurrency.run_in_threadpool, a
    genuinely different thread than whatever thread later reads the
    identity back).

    SINGLE-PROCESS ONLY -- a real constraint, not a caveat to skim past.
    In a multi-node deployment (several gateway/web-app replicas behind a
    load balancer), request N+1 for the same logical session can land on
    a DIFFERENT node than request N, and that node's in-memory dict never
    saw the earlier set_identity() call -- identity silently reverts to
    anonymous, not an error, and Cedar's default-deny means that fails
    closed rather than open, but it's still a correctness bug an operator
    would have to notice and debug. Keeping requests pinned to one node
    (session affinity / sticky routing at the load balancer) OR swapping
    in a real shared backend via configure_identity_store() (Redis, a
    database) is the calling APPLICATION's responsibility -- this module
    doesn't change who owns that decision, it only gives a documented seam
    (configure_identity_store()) for making it, the same way
    web_app.py's own in-memory `sessions` dict already required without
    one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[dict[str, str], list[str]]] = {}

    def get(self, namespaced_key: str) -> tuple[Mapping[str, Any], Sequence[str]] | None:
        with self._lock:
            return self._data.get(namespaced_key)

    def set(self, namespaced_key: str, *, claims: Mapping[str, Any], roles: Sequence[Any]) -> None:
        with self._lock:
            self._data[namespaced_key] = (
                {str(k): str(v) for k, v in claims.items()},
                [str(r) for r in roles],
            )

    def clear(self, namespaced_key: str) -> None:
        with self._lock:
            self._data.pop(namespaced_key, None)


_default_store: IdentityStore = InMemoryIdentityStore()


def configure_identity_store(store: IdentityStore) -> None:
    """Swaps the default store -- e.g. a Redis-backed IdentityStore for a
    multi-replica deployment (see InMemoryIdentityStore's docstring for
    why that's sometimes required, not optional polish). Call once, at
    process startup, before any set_identity()/get_identity()/
    use_identity() call elsewhere -- this is a module-level swap, not
    scoped per call. Untouched, the in-memory default just works for a
    single process or a CLI script."""
    global _default_store
    _default_store = store


def _namespaced(key: str, kind: IdentityKeyKind) -> str:
    return f"{kind.value}:{key}"


def set_identity(
    key: str,
    *,
    kind: IdentityKeyKind = IdentityKeyKind.CUSTOM,
    claims: Mapping[str, Any] | None = None,
    roles: Sequence[Any] | None = None,
) -> None:
    """Stores an identity under (kind, key) in the configured store --
    persists across separate set_identity()/use_identity() calls (and,
    with a real shared backend via configure_identity_store(), across
    processes), unlike current_identity()'s contextvar which only lives
    for one asyncio task. `kind` defaults to CUSTOM so the simplest call
    is just set_identity("my-key", claims=..., roles=...)."""
    _default_store.set(_namespaced(key, kind), claims=claims or {}, roles=roles or [])


def get_identity(
    key: str, *, kind: IdentityKeyKind = IdentityKeyKind.CUSTOM
) -> tuple[dict[str, str], list[str]] | None:
    """Returns the (claims, roles) stored for (kind, key), or None if
    nothing was ever set (or it was cleared) -- the same
    tuple[Mapping, Sequence] | None shape identity_middleware.Extractor
    expects, so this composes directly into IdentityMiddleware's
    extractor= with no adapter needed:
    `extractor=lambda r: get_identity(r.cookies.get("session_id"))`."""
    resolved = _default_store.get(_namespaced(key, kind))
    if resolved is None:
        return None
    claims, roles = resolved
    return dict(claims), list(roles)


def clear_identity(key: str, *, kind: IdentityKeyKind = IdentityKeyKind.CUSTOM) -> None:
    """Removes whatever is stored for (kind, key) -- e.g. on logout.
    A no-op, not an error, if nothing was stored for that key."""
    _default_store.clear(_namespaced(key, kind))


@contextlib.contextmanager
def use_identity(key: str, *, kind: IdentityKeyKind = IdentityKeyKind.CUSTOM) -> Iterator[None]:
    """Looks up (kind, key) in the configured store and enters
    current_identity() for the duration of this block -- a no-op (stays
    anonymous) if nothing is stored for that key, the same
    default-deny-safe behavior as never calling this at all. This is the
    one call every call-shape (web request, CLI command, batch workflow
    step) uses once an identity has been set_identity()'d somewhere
    earlier under the same key."""
    # Lazy, not module-level -- see module docstring for why.
    from parapetai_agent.maf import current_identity

    resolved = get_identity(key, kind=kind)
    if resolved is None:
        yield
        return
    claims, roles = resolved
    with current_identity(claims=claims, roles=roles):
        yield
