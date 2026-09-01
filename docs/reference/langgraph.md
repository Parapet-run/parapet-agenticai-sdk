# `ParapetAgentMiddleware`

`parapetai_agent.langgraph.ParapetAgentMiddleware` — a real
`langchain.agents.middleware.AgentMiddleware` for
`langchain.agents.create_agent(..., middleware=[...])`. See the
[LangGraph / LangChain guide](../frameworks/langgraph.md) for narrative
usage and design rationale; this page is the exhaustive parameter
reference.

```python
from langchain.agents import create_agent
from parapetai_agent.langgraph import build_middleware

agent = create_agent(
    model,
    tools=[lookup_order, hr_lookup],
    middleware=[build_middleware(policy_dir="./policies")],
)
```

There is no `GovernedAgent`/`GovernedRunner`-style subclass here — unlike
MAF's `Agent`/ADK's `Runner`, `create_agent` is a functional construction
API, not a subclassable class, so the middleware object itself is the
entire integration surface.

## `build_middleware()`

```python
def build_middleware(
    policy_dir: str | Path | None = None,
    entities_path: str | Path | None = None,
    agent_id: str | None = None,
    tenant: str = "default",
    control_plane_url: str | None = None,
    agent_secret: str | None = None,
    pep_key_path: str | Path | None = None,
    persist_policy_dir: str | Path | None = None,
    local_log_dir: str | Path | None = None,
    persist_pep_key: bool = True,
    otel_log_mode: Literal["streaming", "buffered"] = "buffered",
    console: bool = True,
) -> ParapetAgentMiddleware:
```

Same kwarg surface, same semantics for every parameter, as
[`build_middleware()`](governed-agent.md#build_middleware)
(MAF)/[`build_plugin()`](governed-runner.md#build_plugin) (ADK) — policy
resolution, control-plane pull, Ed25519 PEP identity, OTel auto-wiring,
idempotent per-identity caching. None of it is LangGraph-specific
(`governance_runtime.py`/`control_plane.py`/`pep_identity.py` are already
framework-agnostic); see those pages for the full explanation of each
parameter.

**One difference from both:** no `alter_transforms=` parameter. ALTER
decisions are not yet supported by this adapter — accepting the kwarg and
silently doing nothing with it would be worse than not accepting it. See
the [Known gaps](../frameworks/langgraph.md#known-gaps) section of the
guide.

Idempotent the same way: calling `build_middleware()` twice with the same
`(policy_dir, entities_path, agent_id, tenant, control_plane_url)` returns
the *same* `ParapetAgentMiddleware` instance rather than starting a second
background poller. `reset_middleware_registry()` (not re-exported under
that bare name from the package root — see below) clears the cache and
joins every poller thread; test-only, mirrors
`parapetai_agent.maf.reset_middleware_registry()` exactly.

## `ParapetAgentMiddleware`

```python
class ParapetAgentMiddleware(AgentMiddleware):
    def __init__(self, engine: PolicyEngine, caller: Caller) -> None: ...
```

Constructed directly only if you're building your own `PolicyEngine`/
`Caller` rather than going through `build_middleware()` — the common case
is `build_middleware()`. Implements:

| Method | Cedar stage | Runs |
|---|---|---|
| `wrap_model_call` / `awrap_model_call` | `model_call` (pre), then `post` | Before and after the underlying model call |
| `wrap_tool_call` / `awrap_tool_call` | `tool_call` | Before the tool body executes |

Each raises `GovernanceDenied` (never a silently-substituted response)
before calling the framework's own `handler(request)` on deny — see
[How a deny surfaces](../frameworks/langgraph.md#what-it-governs).

## Naming: not re-exported from `parapetai_agent` under the bare names

`build_middleware`/`reset_middleware_registry` already belong to
`parapetai_agent.maf`'s top-level re-export — this module's versions are a
genuinely *different* object (a different registry, a different
middleware type), not the same function imported twice, so they aren't
aliased over MAF's at the package root. Reach them as:

```python
from parapetai_agent.langgraph import build_middleware, reset_middleware_registry
# or, from the package root:
from parapetai_agent import reset_langgraph_middleware_registry
```

`ParapetAgentMiddleware`, `agent_identity`, `configure_otel`,
`configure_rotating_audit_log`, `current_identity`, `flush_otel`,
`identity_from_bearer_token`, and `track_tool_denials` **are** re-exported
under their bare names from `parapetai_agent` (shared, framework-agnostic
names — same object either way).

## See also

- [LangGraph / LangChain guide](../frameworks/langgraph.md) — narrative usage, design rationale, known gaps
- [`governed_identity`](governed-identity.md) — per-call end-user identity
- [`Decision`](decision.md), [Exceptions](exceptions.md)
- [Environment variables](env-vars.md)
