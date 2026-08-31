# `GovernedRunner`

`parapetai_agent.adk.GovernedRunner` — `google.adk.runners.Runner` with
Cedar governance wired in automatically. A drop-in replacement for
`Runner(...)`, same rationale as [`GovernedAgent`](governed-agent.md): the
alternative — `build_plugin()` + `plugins=[plugin]` — is genuinely the
entire integration (two lines), but it's opt-in per `Runner(...)` call
site, and forgetting the `plugins=` kwarg means zero enforcement,
silently. See the [ADK guide](../frameworks/adk.md) for narrative usage;
this page is the exhaustive parameter reference.

```python
runner = GovernedRunner(
    agent=root_agent,
    app_name="demo",
    session_service=InMemorySessionService(),
)
```

**Everything past `agent`/`app_name`/`session_service` is optional** — the
minimal call enforces real (if generic) Cedar policy from the moment it's
constructed, using the SDK's own bundled default policy set.

## Constructor

```python
def __init__(
    self,
    *,
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
    alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
    trust_session_user_id: bool = False,
    **kwargs: Any,
) -> None:
```

**Every parameter is keyword-only** — confirmed against `google-adk`'s own
`Runner.__init__` signature, which has no positional parameters to
forward, so this class doesn't invent any either.

`policy_dir` through `alter_transforms` are the exact same governance
surface as [`GovernedAgent`](governed-agent.md#constructor) — identical
meaning, identical defaults, identical control-plane/policy-resolution
behavior. See that page for the full explanation of each. `**kwargs` are
forwarded to `Runner.__init__` — `agent=`, `app_name=`,
`session_service=`, or `app=`, unchanged from plain `Runner(...)`.

## `trust_session_user_id` — the one ADK-specific parameter

| Parameter | Default | Meaning |
|---|---|---|
| `trust_session_user_id` | `False` | Whether ADK's `Session.user_id` — a plain, **unverified** string every `run_async()` call must supply, but never authenticated by ADK itself — flows into Cedar's `identity_claims`. |

MAF has no equivalent, since MAF's `AgentSession` carries no `user_id` at
all. Defaulting this to `True` would be unsafe: it would make
identity-gated Cedar policies silently *stricter* for ADK than for MAF,
since ADK requires a `user_id` unconditionally where MAF's identity is
fully optional — an unverified string would start satisfying identity
checks that should require real, asserted identity (see
[`governed_identity`](governed-identity.md)). Set this `True` only when
your own deployment sets `user_id` from a source you already trust. Not
part of the identity-registry cache key — it affects construction, not
identity.

## `app=` vs. `plugins=`

Confirmed against `google-adk`'s own `Runner._resolve_app()`: passing
**both** `app=` and `plugins=` raises `ValueError` — `plugins=` itself is
deprecated in favor of `App(plugins=[...])`. `GovernedRunner` branches on
which construction style you used:

- If `app=` is present, the governance plugin is appended to
  `app.plugins` in place.
- Otherwise, it goes through the (deprecated) `plugins=` kwarg.

Either way, any plugins you pass explicitly run **alongside** the
governance plugin — ADK invokes every registered plugin's matching
callback for a given hook point; there's no single-plugin-wins ordering
the way MAF's middleware chain has.

## `InMemoryGovernedRunner`

```python
class InMemoryGovernedRunner(GovernedRunner):
    def __init__(
        self,
        agent: AdkBaseAgent | None = None,
        *,
        app_name: str | None = None,
        **kwargs: Any,
    ) -> None:
```

`GovernedRunner` + `google.adk.runners.InMemoryRunner`'s own convenience
defaults, combined — for the common case of wanting the simplest possible
construction with no real session/artifact/memory backend. Real ADK
samples commonly use `InMemoryRunner(agent=..., plugins=[...])` directly,
not the fully-spelled-out `Runner(session_service=..., artifact_service=...,
...)` — `GovernedRunner` alone doesn't mirror that convenience, since it
subclasses `Runner` directly, not `InMemoryRunner`. Use this instead of
`GovernedRunner` whenever your own code would otherwise reach for
`InMemoryRunner`.

| Parameter | Default | Meaning |
|---|---|---|
| `agent` | `None`, positional | Optional ADK `BaseAgent`. |
| `app_name` | `None`, keyword | Defaults to `"InMemoryRunner"` — but only if neither `app_name` nor `kwargs["app"]` is given. |
| `**kwargs` | — | Forwarded to `GovernedRunner.__init__` — so the same governance kwargs (`policy_dir`, `agent_id`, `control_plane_url`, `trust_session_user_id`, ...) work exactly as they do on `GovernedRunner` itself. `artifact_service`/`memory_service`/`session_service` are pre-filled with fresh `InMemory*` instances via `setdefault` — pass any of them explicitly to override just that one and keep the in-memory convenience for the rest. |

```python
from parapetai_agent.adk import InMemoryGovernedRunner

runner = InMemoryGovernedRunner(agent=root_agent, policy_dir="./policies")
```

## `build_plugin()`

The lower-level function `GovernedRunner` builds on:

```python
def build_plugin(
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
    alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
    trust_session_user_id: bool = False,
) -> ParapetPlugin:
```

The ADK equivalent of `build_middleware()` — same kwarg surface, same
semantics for every parameter (policy resolution, control-plane pull,
Ed25519 PEP identity, OTel auto-wiring, idempotent per-identity caching;
`control_plane.py`/`pep_identity.py`/`governance_runtime.py` are already
framework-agnostic). Returns a single `ParapetPlugin`:

```python
from google.adk.runners import Runner
from parapetai_agent.adk import build_plugin

plugin = build_plugin(policy_dir="./policies")
runner = Runner(agent=root_agent, app_name="demo", session_service=..., plugins=[plugin])
```

## How a deny surfaces

`GovernedRunner` never raises — it uses ADK's own "early exit" callback
contract. See the [ADK guide](../frameworks/adk.md#how-a-deny-surfaces)
for the full explanation and streaming behavior.

## See also

- [ADK guide](../frameworks/adk.md) — narrative usage, streaming behavior
- [`governed_identity`](governed-identity.md) — per-call end-user identity
- [`Decision`](decision.md), [Exceptions](exceptions.md)
- [Environment variables](env-vars.md)
