"""identity_store.py -- the keyed persistence layer current_identity()/
IdentityMiddleware don't provide on their own. Proves: the in-memory
default works with zero setup; set/get/clear round-trip; SESSION vs
CUSTOM with the same string value don't collide; use_identity() actually
enters current_identity() (and is a safe no-op for an unknown key); and
configure_identity_store() really does swap the backend, not just accept
one.

Run: uv run pytest parapetai-agent/tests/test_identity_store.py -v
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from parapetai_agent.identity_store import (
    IdentityKeyKind,
    clear_identity,
    configure_identity_store,
    get_identity,
    set_identity,
    use_identity,
)
from parapetai_agent.maf import _identity_claims, _identity_roles

# Per-test store isolation is handled by conftest.py's autouse
# _reset_identity_store fixture, not repeated here.


def test_unknown_key_returns_none_not_an_error() -> None:
    assert get_identity("nobody") is None


def test_set_then_get_round_trips() -> None:
    set_identity("alice", claims={"oid": "abc"}, roles=["OrderViewer"])
    assert get_identity("alice") == ({"oid": "abc"}, ["OrderViewer"])


def test_clear_removes_it() -> None:
    set_identity("alice", claims={"oid": "abc"}, roles=[])
    clear_identity("alice")
    assert get_identity("alice") is None


def test_clear_unknown_key_is_a_no_op() -> None:
    clear_identity("nobody")  # must not raise


def test_session_and_custom_with_the_same_value_do_not_collide() -> None:
    set_identity("123", kind=IdentityKeyKind.SESSION, claims={"who": "session-123"}, roles=[])
    set_identity("123", kind=IdentityKeyKind.CUSTOM, claims={"who": "custom-123"}, roles=[])
    assert get_identity("123", kind=IdentityKeyKind.SESSION) == ({"who": "session-123"}, [])
    assert get_identity("123", kind=IdentityKeyKind.CUSTOM) == ({"who": "custom-123"}, [])


def test_use_identity_enters_current_identity_for_a_known_key() -> None:
    set_identity("alice", claims={"oid": "abc"}, roles=["OrderViewer"])
    assert _identity_claims(None) == {}
    with use_identity("alice"):
        assert _identity_claims(None) == {"oid": "abc"}
        assert _identity_roles(None) == ["OrderViewer"]
    assert _identity_claims(None) == {}


def test_use_identity_is_a_no_op_for_an_unknown_key() -> None:
    with use_identity("nobody"):
        assert _identity_claims(None) == {}
        assert _identity_roles(None) == []


def test_configure_identity_store_swaps_the_backend() -> None:
    class _FakeStore:
        def __init__(self) -> None:
            self.set_calls: list[str] = []

        def get(self, namespaced_key: str) -> tuple[Mapping[str, Any], Sequence[str]] | None:
            return ({"fake": "true"}, []) if namespaced_key == "custom:probe" else None

        def set(
            self, namespaced_key: str, *, claims: Mapping[str, Any], roles: Sequence[Any]
        ) -> None:
            self.set_calls.append(namespaced_key)

        def clear(self, namespaced_key: str) -> None:
            pass

    fake = _FakeStore()
    configure_identity_store(fake)
    assert get_identity("probe") == ({"fake": "true"}, [])
    set_identity("other", claims={}, roles=[])
    assert fake.set_calls == ["custom:other"]
