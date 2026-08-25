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
"""

from __future__ import annotations

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
    from parapetai_agent.maf import GovernanceDenied as GovernanceDenied
    from parapetai_agent.maf import GovernedAgent as GovernedAgent
    from parapetai_agent.maf import build_middleware as build_middleware
    from parapetai_agent.maf import configure_otel as configure_otel
    from parapetai_agent.maf import configure_rotating_audit_log as configure_rotating_audit_log
    from parapetai_agent.maf import current_identity as current_identity
    from parapetai_agent.maf import flush_otel as flush_otel
    from parapetai_agent.maf import governed_identity as governed_identity
    from parapetai_agent.maf import (
        identity_from_azure_credential as identity_from_azure_credential,
    )
    from parapetai_agent.maf import reset_middleware_registry as reset_middleware_registry
    from parapetai_agent.maf import track_tool_denials as track_tool_denials

    __all__ += [
        "DEFAULT_ALTER_TRANSFORMS",
        "GovernanceDenied",
        "GovernedAgent",
        "build_middleware",
        "configure_otel",
        "configure_rotating_audit_log",
        "current_identity",
        "flush_otel",
        "governed_identity",
        "identity_from_azure_credential",
        "reset_middleware_registry",
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
