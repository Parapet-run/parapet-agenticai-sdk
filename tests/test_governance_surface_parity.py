"""Cross-integration API-surface parity: catches the exact bug class that
shipped GovernedAgent/GovernedRunner without vendor_scoped_resources while
build_middleware()/build_plugin() already had it (a real gap, fixed
alongside this test) -- a new opt-in flag added to GovernanceHook's
constructor but not mirrored on every wrapper/adapter that builds one.

MECHANICAL, not a hand-maintained checklist: every "flag" this file checks
is derived directly from GovernanceHook.__init__'s own signature (anything
beyond engine/caller/on_decision) via `_hook_flags()` below. Add a new
opt-in constructor flag to GovernanceHook and this file starts asserting
every integration surface accepts it too, with zero edits needed here --
you only touch this file to add a new integration surface, or to add an
entry to _EXEMPT when a surface has a documented, deliberate reason not to
accept a given flag (see Governor.from_control_plane's entry below for the
shape that takes).

Skips per-integration when its extra isn't installed, so the base dev env
stays light -- same convention test_conformance_frameworks.py uses. CI
(`make test-sdk`) installs maf+adk+langgraph, so the real assertions run
there.

Also carries a second, narrower property: every such flag must be
mentioned SOMEWHERE under docs/ at all -- the exact gap this flag's own
addition found (auth-integrations.md's vendor/CRUD/corroboration/
cost-tracking work landed with zero docs/ coverage for months). This
catches "completely undocumented," not "adequately documented" -- judging
prose quality/completeness stays a human (or code-review) job; see
CLAUDE.md's "Documenting a new governance flag" working agreement for the
part this test can't automate.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from parapetai_agent.govern import Governor
from parapetai_agent.policy.hooks import GovernanceHook

REPO_ROOT = Path(__file__).resolve().parents[1]

_HOOK_BASE_PARAMS = {"self", "engine", "caller", "on_decision"}


def _hook_flags() -> set[str]:
    """Every opt-in constructor flag GovernanceHook itself accepts, beyond
    the three every integration already threads through identically
    (engine/caller/on_decision). Currently just vendor_scoped_resources --
    grows automatically as GovernanceHook grows, no edit needed here."""
    return set(inspect.signature(GovernanceHook.__init__).parameters) - _HOOK_BASE_PARAMS


def _params(target: Callable[..., Any]) -> set[str]:
    return set(inspect.signature(target).parameters)


# (surface name, callable, missing-flag reason-or-None). None means "must
# accept every GovernanceHook flag"; a string documents a DELIBERATE,
# reasoned exception -- see this module's own docstring.
_EXEMPT: dict[str, str] = {
    "Governor.from_control_plane": (
        "Always resolves vendor_scoped_resources from the bundle's own "
        "field (Bootstrap.vendor_scoped_resources) once a control plane "
        "is configured at all -- same priority rule build_middleware()/"
        "build_plugin() use. A caller-facing override here would be dead "
        "code by construction, so it's deliberately absent, not missing. "
        "See docs/reference/governor.md's from_control_plane() section."
    ),
}


def _governance_surfaces() -> list[tuple[str, Callable[..., Any]]]:
    """Every place in this SDK that constructs (or wraps something that
    constructs) a GovernanceHook. Each entry is (name, the __init__/
    classmethod/function whose signature should carry every hook flag)."""
    surfaces: list[tuple[str, Callable[..., Any]]] = [
        ("Governor.__init__", Governor.__init__),
        ("Governor.from_policy_dir", Governor.from_policy_dir),
        ("Governor.from_control_plane", Governor.from_control_plane),
    ]

    try:
        from parapetai_agent import maf
    except ImportError:
        pass
    else:
        surfaces += [
            ("maf.build_middleware", maf.build_middleware),
            ("maf.GovernedAgent.__init__", maf.GovernedAgent.__init__),
            ("maf.ParapetChatMiddleware.__init__", maf.ParapetChatMiddleware.__init__),
            ("maf.ParapetFunctionMiddleware.__init__", maf.ParapetFunctionMiddleware.__init__),
        ]

    try:
        from parapetai_agent import adk
    except ImportError:
        pass
    else:
        surfaces += [
            ("adk.build_plugin", adk.build_plugin),
            ("adk.GovernedRunner.__init__", adk.GovernedRunner.__init__),
            ("adk.ParapetPlugin.__init__", adk.ParapetPlugin.__init__),
        ]

    try:
        from parapetai_agent import langgraph
    except ImportError:
        pass
    else:
        surfaces += [
            ("langgraph.build_middleware", langgraph.build_middleware),
            (
                "langgraph.ParapetAgentMiddleware.__init__",
                langgraph.ParapetAgentMiddleware.__init__,
            ),
        ]

    return surfaces


@pytest.mark.parametrize(
    ("name", "target"), _governance_surfaces(), ids=[n for n, _ in _governance_surfaces()]
)
def test_every_integration_surface_accepts_every_hook_flag(
    name: str, target: Callable[..., Any]
) -> None:
    hook_flags = _hook_flags()
    assert hook_flags, "GovernanceHook grew no flags at all -- this test would be vacuous"
    surface_params = _params(target)
    missing = hook_flags - surface_params
    if name in _EXEMPT:
        assert missing, (
            f"{name} is in _EXEMPT (reason: {_EXEMPT[name]!r}) but now accepts every "
            "hook flag -- remove the exemption, it's no longer needed."
        )
        return
    assert not missing, (
        f"{name} is missing GovernanceHook flag(s) {sorted(missing)}. Every integration "
        "surface must mirror every opt-in GovernanceHook constructor flag (see this "
        "module's own docstring) -- add the parameter and forward it to the "
        "GovernanceHook(...)/build_middleware()/build_plugin() call this surface makes "
        "internally, or add a reasoned entry to _EXEMPT if there's a deliberate reason "
        "not to (see Governor.from_control_plane's own entry for the shape)."
    )


def test_every_hook_flag_is_mentioned_somewhere_in_docs() -> None:
    """Not "is this well-documented" (that needs a human/reviewer) -- just
    "is this mentioned at all," which is exactly the gap that shipped
    vendor_calls.py/corroboration.py/cost_tracker.py with zero docs/
    coverage for months. A grep this blunt would have caught that
    instantly."""
    docs_dir = REPO_ROOT / "docs"
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in docs_dir.rglob("*.md"))
    for flag in sorted(_hook_flags()):
        assert flag in corpus, (
            f"GovernanceHook's {flag!r} constructor flag is not mentioned anywhere under "
            "docs/. A new opt-in governance flag needs a documented home before it ships "
            "-- see docs/reference/vendor-calls.md for the shape of an existing one, and "
            "CLAUDE.md's 'Documenting a new governance flag' working agreement."
        )
