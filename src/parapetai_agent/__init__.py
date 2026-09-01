"""parapetai-agent: open-source in-process governance for AI agent frameworks.

The whole point of this top-level module is that an agent author needs
exactly one import line:

    from parapetai_agent import GovernedAgent as Agent, GovernanceDenied, identity_from_claims

`GovernedAgent` is a drop-in `agent_framework.Agent` replacement -- pass
the same `policy_dir=`/`agent_id=`/`control_plane_url=`/`agent_secret=`
kwargs you'd otherwise hand to `build_middleware()` directly into the
`Agent(...)` construction you already write. `build_middleware()` itself
is also exported, for callers priming its identity-keyed cache once at
process startup (see its own docstring) or building `middleware=[...]`
by hand for a framework this package doesn't have a dedicated subclass
for yet.

`set_identity`/`get_identity`/`clear_identity`/`use_identity` (see
parapetai_agent/identity_store.py) persist an identity across separate calls
under a developer-chosen key -- the CLI/batch-workflow answer to ambient
identity, no web framework involved:

    set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])
    with use_identity("alice"):
        await agent.run(...)

`IdentityMiddleware`/`jwt_bearer_extractor` require the optional `web`
extra (`pip install parapetai-agent[web]`) and are imported lazily/guarded
here so `from parapetai_agent import GovernedAgent` keeps working for a
non-web consumer (a CLI script, a background worker) that never installed
Starlette -- see parapetai_agent/identity_middleware.py's own docstring.

`GovernedAgent`/`build_middleware`/etc. themselves require the optional
`maf` extra (`agent-framework`, `mcp`, the OpenTelemetry SDK) and are
guarded the same way, for a stricter reason than the web case: this
package also carries the shared foundation (Cedar engine, Snapshot/Caller,
the PEP<->control-plane protocol client, signing) that `gateway/` and
`control-plane/` import directly -- e.g. `from parapetai_agent.signing
import signing_payload`. Any import of ANY submodule runs this
__init__.py first, so an unconditional `from parapetai_agent.maf import
...` here would make `agent_framework` a hard runtime dependency of the
gateway and control plane too, exactly what CLAUDE.md's "never a runtime
dependency of the core gateway" rule forbids. A prior version of this
file did that unconditionally and broke the control-plane Docker build
(`--no-dev`, no `maf` extra installed) with `ModuleNotFoundError:
No module named 'agent_framework'` -- keep this guarded.

`GovernedRunner`/`build_plugin`/etc. (parapetai_agent/adk.py, Google ADK)
are a SEPARATE framework integration behind its own `adk` extra
(`google-adk`, again the OpenTelemetry SDK) -- guarded the identical way,
and deliberately independent of the `maf` guard above: `pip install
parapetai-agent[adk]` alone must work without ever importing
`agent_framework`, and vice versa. Shared names that mean the exact same
thing regardless of which framework you picked (GovernanceDenied,
configure_otel, configure_rotating_audit_log, flush_otel,
track_tool_denials, current_identity, identity_from_bearer_token,
agent_identity) are re-exported from BOTH blocks -- harmless, since
maf.py and adk.py both source them from the same underlying
parapetai_agent.governance_runtime/scoped_data modules, so whichever
block's import wins binds the identical object either way (verified via
mypy --strict, which would flag a real type mismatch if it weren't).
`governed_identity` is the one exception: maf.py defines its OWN,
strictly richer version (adds a `credential=` source, for azure-identity
credentials -- see maf.py's own docstring) rather than re-exporting
scoped_data's base one, so the two are NOT interchangeable objects (mypy
caught this as a real "Incompatible import" error when both were
re-exported here under the same name). This top level therefore keeps
`maf.py`'s version at `parapetai_agent.governed_identity` (the
established name, unchanged), and does not export the adk block's own
-- reach it as `parapetai_agent.adk.governed_identity` (claims/roles/token
sources only, no `credential=`) if you specifically want ADK's without
also installing `maf`.
`GovernedAgent`/`build_middleware`/DEFAULT_ALTER_TRANSFORMS (MAF's own,
agent_framework-shaped) and `GovernedRunner`/`build_plugin` (ADK's own,
google-adk-shaped) are each framework's OWN names, not unified -- see
adk.py's module docstring for why forcing one shared "GovernedX" name
across frameworks whose own architecture puts the governable seam on a
different class would be misleading, not just a naming preference.
`parapetai_agent.adk.DEFAULT_ALTER_TRANSFORMS` is intentionally NOT
re-exported at this top level (it would collide with maf.py's own,
non-identical, ChatResponse-shaped default) -- import it from
`parapetai_agent.adk` directly if you need ADK's specifically.

`ParapetAgentMiddleware`/`parapetai_agent.langgraph.build_middleware`
(LangGraph/LangChain's own, `langchain.agents.create_agent(...,
middleware=[...])`-shaped) is the third framework integration, behind its
own `langgraph` extra -- same "no forced shared name" reasoning: LangChain's
own construction API is functional (`create_agent(model, tools,
middleware=[...])`), not a subclassable `Agent`/`Runner`, so there is no
`GovernedX` class here to begin with, only the middleware object itself.
`build_middleware`/`reset_middleware_registry` are NOT re-exported at this
top level under those bare names -- both already belong to MAF's block
above and are a genuinely different object per module, unlike the shared
governance_runtime/scoped_data names re-exported from every block. Reach
LangGraph's own via `parapetai_agent.langgraph.build_middleware` /
`parapetai_agent.reset_langgraph_middleware_registry`.
"""

from __future__ import annotations

from parapetai_agent.govern import GovernanceDenied, GovernanceReviewRequired, Governor
from parapetai_agent.identity_store import (
    IdentityKeyKind,
    IdentityStore,
    InMemoryIdentityStore,
    clear_identity,
    configure_identity_store,
    get_identity,
    set_identity,
    use_identity,
)
from parapetai_agent.token_identity import identity_from_claims

__all__ = [
    # Framework-neutral entry point -- works with no framework at all.
    "GovernanceDenied",
    "GovernanceReviewRequired",
    "Governor",
    "IdentityKeyKind",
    "IdentityStore",
    "InMemoryIdentityStore",
    "clear_identity",
    "configure_identity_store",
    "get_identity",
    "identity_from_claims",
    "set_identity",
    "use_identity",
]

try:
    # `as X`, not a plain import: ruff's F401 can't see the dynamic
    # __all__ += [...] below as a use, so this is the standard
    # explicit-re-export spelling that tells it these names are
    # intentionally re-exported, not dead. See module docstring for why
    # this whole block must stay guarded.
    from parapetai_agent.maf import DEFAULT_ALTER_TRANSFORMS as DEFAULT_ALTER_TRANSFORMS
    from parapetai_agent.maf import GovernedAgent as GovernedAgent
    from parapetai_agent.maf import agent_identity as agent_identity
    from parapetai_agent.maf import build_middleware as build_middleware
    from parapetai_agent.maf import configure_otel as configure_otel
    from parapetai_agent.maf import configure_rotating_audit_log as configure_rotating_audit_log
    from parapetai_agent.maf import current_identity as current_identity
    from parapetai_agent.maf import flush_otel as flush_otel
    from parapetai_agent.maf import governed_identity as governed_identity
    from parapetai_agent.maf import (
        identity_from_azure_credential as identity_from_azure_credential,
    )
    from parapetai_agent.maf import identity_from_bearer_token as identity_from_bearer_token
    from parapetai_agent.maf import reset_middleware_registry as reset_middleware_registry
    from parapetai_agent.maf import track_tool_denials as track_tool_denials

    __all__ += [
        "DEFAULT_ALTER_TRANSFORMS",
        "GovernedAgent",
        "agent_identity",
        "build_middleware",
        "configure_otel",
        "configure_rotating_audit_log",
        "current_identity",
        "flush_otel",
        "governed_identity",
        "identity_from_azure_credential",
        "identity_from_bearer_token",
        "reset_middleware_registry",
        "track_tool_denials",
    ]
except ImportError:
    pass

try:
    # Same guarded-import discipline, for the `adk` extra -- see module
    # docstring's second paragraph for which names are shared with the
    # `maf` block above vs. genuinely ADK-only.
    from parapetai_agent.adk import GovernedRunner as GovernedRunner
    from parapetai_agent.adk import InMemoryGovernedRunner as InMemoryGovernedRunner
    from parapetai_agent.adk import agent_identity as agent_identity
    from parapetai_agent.adk import build_plugin as build_plugin
    from parapetai_agent.adk import configure_otel as configure_otel
    from parapetai_agent.adk import configure_rotating_audit_log as configure_rotating_audit_log
    from parapetai_agent.adk import current_identity as current_identity
    from parapetai_agent.adk import flush_otel as flush_otel
    from parapetai_agent.adk import identity_from_bearer_token as identity_from_bearer_token
    from parapetai_agent.adk import reset_plugin_registry as reset_plugin_registry
    from parapetai_agent.adk import track_tool_denials as track_tool_denials

    # governed_identity deliberately NOT re-exported here -- see module
    # docstring's "governed_identity is the one exception" paragraph.
    # parapetai_agent.adk.governed_identity is still reachable directly.

    __all__ += [
        "GovernedRunner",
        "InMemoryGovernedRunner",
        "agent_identity",
        "build_plugin",
        "configure_otel",
        "configure_rotating_audit_log",
        "current_identity",
        "flush_otel",
        "identity_from_bearer_token",
        "reset_plugin_registry",
        "track_tool_denials",
    ]
except ImportError:
    pass

try:
    # Same guarded-import discipline, for the `langgraph` extra -- needs
    # the full `langchain` package (langchain.agents.middleware), not just
    # langgraph/langchain-core -- see langgraph.py's own module docstring.
    #
    # `build_middleware` and `reset_middleware_registry` are NOT re-exported
    # under those bare names here -- both already belong to the `maf` block
    # above, and unlike the shared governance_runtime/scoped_data names
    # below, these are a DIFFERENT object per module (a different registry,
    # a different middleware type), not the same underlying function
    # re-imported twice. Same resolution the adk block already uses for its
    # own colliding `governed_identity` (see that block's comment) --
    # keep the colliding name reachable module-qualified
    # (`parapetai_agent.langgraph.build_middleware`) rather than picking an
    # arbitrary winner at this top level.
    from parapetai_agent.langgraph import ParapetAgentMiddleware as ParapetAgentMiddleware
    from parapetai_agent.langgraph import agent_identity as agent_identity
    from parapetai_agent.langgraph import configure_otel as configure_otel
    from parapetai_agent.langgraph import (
        configure_rotating_audit_log as configure_rotating_audit_log,
    )
    from parapetai_agent.langgraph import current_identity as current_identity
    from parapetai_agent.langgraph import flush_otel as flush_otel
    from parapetai_agent.langgraph import identity_from_bearer_token as identity_from_bearer_token
    from parapetai_agent.langgraph import (
        reset_middleware_registry as reset_langgraph_middleware_registry,
    )
    from parapetai_agent.langgraph import track_tool_denials as track_tool_denials

    # governed_identity deliberately NOT re-exported here either -- same
    # reasoning as the adk block's own comment above.
    # parapetai_agent.langgraph.governed_identity is still reachable
    # directly (scoped_data's base version, no `credential=`, same as
    # ADK's).

    __all__ += [
        "ParapetAgentMiddleware",
        "agent_identity",
        "configure_otel",
        "configure_rotating_audit_log",
        "current_identity",
        "flush_otel",
        "identity_from_bearer_token",
        "reset_langgraph_middleware_registry",
        "track_tool_denials",
    ]
except ImportError:
    pass

try:
    # Same reasoning: needs the optional `web` extra (starlette).
    from parapetai_agent.identity_middleware import IdentityMiddleware as IdentityMiddleware
    from parapetai_agent.identity_middleware import jwt_bearer_extractor as jwt_bearer_extractor

    __all__ += ["IdentityMiddleware", "jwt_bearer_extractor"]
except ImportError:
    pass
