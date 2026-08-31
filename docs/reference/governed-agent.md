# `GovernedAgent`

`parapetai_agent.GovernedAgent` (defined in `maf.py`) — `agent_framework.Agent`
with Cedar governance wired in automatically. A drop-in replacement for
`Agent(...)`. See the [MAF guide](../frameworks/maf.md) for narrative
usage; this page is the exhaustive parameter reference, shared with
[`build_middleware()`](#build_middleware) below.

```python
async with GovernedAgent(
    client=client,
    name="workplace-agent",
    instructions="...",
    tools=[...],
) as agent:
    ...
```

**Everything past `client`/`name`/`instructions`/`tools` is optional** —
the minimal call enforces real (if generic) Cedar policy from the moment
it's constructed, using the SDK's own bundled default policy set.

## Why this class exists, not just `build_middleware()`

`build_middleware()` + `middleware=[chat_mw, func_mw]` is genuinely the
entire integration — three lines — but it's opt-in *per* `Agent(...)`
call site. Forget the `middleware=` kwarg and there is zero enforcement,
**silently**: Cedar's own default-deny only ever applies once a decision
is actually evaluated, and nothing forces that to happen. `GovernedAgent`
removes that failure mode — governance is constructed and attached inside
`__init__`, not left to be remembered at every call site.

This is deliberately **not** a process-wide monkeypatch of
`agent_framework.Agent.__init__`. That would make every `Agent(...)`
anywhere in a process governed with no visible change at the call site —
a stronger default, but it trades away the explicitness this codebase
otherwise insists on. `GovernedAgent` still requires touching each call
site — swap the import — but that's one visible, one-line change instead
of three easy-to-forget ones.

Any middleware passed explicitly via `middleware=[...]` runs **after**
the governance middleware — Cedar decides first, not instead.

## Constructor

```python
def __init__(
    self,
    *args: Any,
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
    **kwargs: Any,
) -> None:
```

`*args`/`**kwargs` are forwarded to `agent_framework.Agent.__init__` after
`middleware` is prepended with the governance middleware pair —
`client=`, `name=`, `instructions=`, `tools=` all go here, unchanged from
plain `Agent(...)`.

### Policy source

| Parameter | Default | Meaning |
|---|---|---|
| `policy_dir` | `None` | Local Cedar policy directory. Omitted → the SDK's bundled default policy set (permit `model_call`/`tool_call`, narrow permit on `http_request`; deny everything else). Pass a real directory for your own local/air-gapped policies, or to override the bundled starting point. |
| `entities_path` | `None` | Optional Cedar entities JSON, paired with `policy_dir`. |

### Identity

| Parameter | Default | Meaning |
|---|---|---|
| `agent_id` | `None` | This agent's identifier for Cedar's principal. Optional, mirroring the HTTP gateway's own fallback: no explicit id → `PARAPETAI_AGENT_ID` env var → `Agent::"anonymous"` — **still evaluated under Cedar's default-deny, never a bypass.** Omit it if a real Service Principal identity will arrive later via a token (see [`governed_identity`](governed-identity.md)'s `credential=`), which overrides this static id for any decision made while set. |
| `tenant` | `"default"` | Tenant string used when constructing the `Caller`. |

### Control plane (all opt-in)

| Parameter | Default | Meaning |
|---|---|---|
| `control_plane_url` | `None` | Falls back to `PARAPETAI_CONTROL_PLANE_URL`. |
| `agent_secret` | `None` | Falls back to `PARAPETAI_AGENT_SECRET`. |
| `pep_key_path` | `None` | Overrides where this PEP's Ed25519 keypair is stored/loaded (default: `PARAPETAI_PEP_KEY_PATH` env var, else `~/.parapetai/pep_ed25519.key`). Pass an explicit path for test isolation, or to run multiple distinct PEP identities from one host. |
| `persist_policy_dir` | `None` | See [Cloud vs. local resolution](#cloud-vs-local-policy-resolution) below. |
| `persist_pep_key` | `True` | `False` skips disk entirely for the PEP keypair — a fresh, never-written keypair every call, for a process with no writable filesystem at all; `pep_key_path` is ignored in that case. Trades identity stability across restarts (a control-plane-initiated key rotation becomes a no-op) for that, not correctness. |

When both `control_plane_url`/`agent_secret` resolve (directly or via
env), the named agent's real bundle **replaces** whatever `policy_dir`
resolved to (the bundled default, if `policy_dir` was omitted).

### Observability

| Parameter | Default | Meaning |
|---|---|---|
| `local_log_dir` | `None` | When given, wires up a rotating audit-log file automatically — one less required call at your own application's top level. Omitted: no local file sink (console output, if `console=True`, is unaffected either way). |
| `otel_log_mode` | `"buffered"` | Passed straight through to OTel's own `log_mode=`, when OTel auto-wiring fires (only when `control_plane_url`/`agent_secret` both resolve and OTel isn't already configured elsewhere in the process). `"streaming"` is the alternative. |
| `console` | `True` | Governs **both** console outputs uniformly — the `local_log_dir` file sink's own stream-to-stdout, and the auto-wired OTel call's own console output. `False` suppresses both (no structlog JSON lines, no raw OTel dump printed) — useful for a CLI script whose own printed output shouldn't be interleaved with a raw decision/telemetry stream. The file sink and anything actually shipped to a control plane are **unaffected** either way; this only controls what prints locally. |

OpenTelemetry is wired up automatically once `control_plane_url`/
`agent_secret` resolve — no separate `configure_otel()` call is required
to see Cedar decisions show up in the control plane's OTel log table. If
your own application already called `configure_otel()` earlier, this
auto-wiring detects that and steps aside — whichever call happens first
wins, since OTel's provider registration is process-wide and set-once.

### Response transforms

| Parameter | Default | Meaning |
|---|---|---|
| `alter_transforms` | `None` | Named callables a post-call `ALTER` decision (a bundle permit carrying `@action("alter")` + `@alter_with("<name>")`) applies to a model response or tool result before it's let through. Merged **over** the built-in defaults (currently just `"redact_all"`, a placeholder). A bundle naming a transform not registered here fails closed to a deny — never a silent pass-through of the original, unaltered content. |

## Cloud vs. local policy resolution

`persist_policy_dir` controls whether a fetched control-plane bundle is
written to disk:

- **Given**: a *synchronous* fetch-and-write to `persist_policy_dir`
  happens first, then the policy engine reads from there — a real on-disk
  cache that survives a restart even if the control plane is briefly
  unreachable. Fails closed (raises) if that first fetch fails with
  nothing yet on disk.
- **Omitted (default)**: the policy engine is constructed *first* from
  `policy_dir` (the bundled default, unless overridden) — so it's already
  enforcing something real — then the real bundle is fetched and applied
  directly to that engine's memory, no disk write. If the fetch fails, the
  engine keeps serving `policy_dir`'s policies rather than crashing the
  process — deliberately more resilient for a serverless/k8s cold start,
  where crashing on a transient control-plane hiccup is worse than
  briefly enforcing a known-safe baseline until the next poll succeeds.

## Idempotent per identity

`build_middleware()` (and therefore `GovernedAgent`) is idempotent, keyed
by `(resolved policy_dir, resolved entities_path, agent_id, tenant,
control_plane_url)`: calling it once, or a thousand times, for the same
resolved identity returns the same `PolicyEngine`/middleware pair and
starts no new background thread — verified against a real failure mode
(a web server sharing one governed agent across many chat sessions would
otherwise spawn a fresh poller thread per session). A *different* key
always gets its own independent engine/middleware/thread.
`persist_policy_dir`/`local_log_dir`/`alter_transforms` are **not** part
of that key — they affect construction, not identity.

## `build_middleware()`

The lower-level function `GovernedAgent` builds on — use this directly
only when you can't subclass `Agent` (or don't want to):

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
    alter_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
) -> tuple[ParapetChatMiddleware, ParapetFunctionMiddleware]:
```

Every parameter has identical meaning to the `GovernedAgent` constructor
above. Returns the `(chat_middleware, function_middleware)` pair — one
`PolicyEngine`, one `Caller`, both middleware, the pairing this module is
designed around so a tool-call decision can see which model call preceded
it:

```python
from agent_framework import Agent
from parapetai_agent.maf import build_middleware

chat_mw, func_mw = build_middleware(policy_dir="./policies")
agent = Agent(client=client, name="...", instructions="...", middleware=[chat_mw, func_mw])
```

## How a deny surfaces

Asymmetric by design — see the [MAF guide](../frameworks/maf.md#how-a-deny-surfaces)
for the full explanation of why the model-call and tool-call layers
differ.

## See also

- [MAF guide](../frameworks/maf.md) — narrative usage, streaming behavior
- [`governed_identity` (MAF variant)](governed-identity.md#parapetai_agentmafgoverned_identity) — per-call end-user identity
- [`Decision`](decision.md), [Exceptions](exceptions.md)
- [Environment variables](env-vars.md)
