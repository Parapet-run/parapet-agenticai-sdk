# `Governor`

`parapetai_agent.Governor` (defined in `govern.py`) — a framework-neutral
governance entry point over one policy set. Construct it once (from a
local policy dir, or an in-memory bundle), then call `check_input()` /
`authorize_tool()` / `check_output()` from wherever your agent loop fires.
This is what LangGraph, CrewAI, the OpenAI Agents SDK, or a bare `while`
loop use — no adapter, no framework dependency. See
[Governor guide](../frameworks/governor.md) for narrative usage; this page
is the exhaustive parameter reference.

Identity is passed per call (`roles=`, `claims=`); with none supplied the
caller is unauthenticated, which Cedar evaluates under its default-deny
policy set — never a bypass.

## Constructors

`Governor` is built via one of two classmethods, not `__init__` directly
in normal use.

### `Governor.from_policy_dir()`

```python
@classmethod
def from_policy_dir(
    cls,
    policy_dir: str | Path,
    entities_path: str | Path | None = None,
    *,
    bundle_files: Mapping[str, str] | None = None,
    caller: Caller | None = None,
    on_decision: OnDecision | None = None,
) -> Governor:
```

Load Cedar policy from local files. Fully local — no network call, ever.

| Parameter | Default | Meaning |
|---|---|---|
| `policy_dir` | required, positional | Local directory of `.cedar` policy files. |
| `entities_path` | `None`, positional | Optional Cedar entities JSON file. |
| `bundle_files` | `None`, keyword | The same `{filename: content}` shape a control-plane bundle carries. When supplied, loaded into fresh `ContentCheckConfig`/`GroundednessConfig`/`JudgeConfig` instances — this is what enables the input scanners (PII/secrets/injection) and post-model evals (groundedness, SLM judge). Without it, only Cedar authorization runs. |
| `caller` | `None`, keyword | This process's own identity. Defaults to `Caller(agent_id="agent")`. |
| `on_decision` | `None`, keyword | Callback fired for every decision — see [`Decision`](decision.md#getting-a-decision-out-of-a-call). |

```python
gov = Governor.from_policy_dir("./policies")
```

### `Governor.from_control_plane()`

```python
@classmethod
def from_control_plane(
    cls,
    control_plane_url: str | None = None,
    agent_secret: str | None = None,
    *,
    policy_dir: str | Path,
    entities_path: str | Path | None = None,
    persist_policy_dir: str | Path | None = None,
    pep_key_path: str | Path | None = None,
    agent_id: str | None = None,
    tenant: str = "default",
    mode: str = "enforce",
    caller: Caller | None = None,
    on_decision: OnDecision | None = None,
) -> Governor:
```

Govern from control-plane-authored policy, refreshed in the background —
the framework-neutral equivalent of `build_middleware(control_plane_url=,
agent_secret=)`. Fetches the signed bundle, applies it, and starts a
background poller so later edits and approvals land without a restart.
**Every decision is still evaluated locally, in-process** — the control
plane is never on the decision path, so it being down never blocks a
single call.

| Parameter | Default | Meaning |
|---|---|---|
| `control_plane_url` | `None`, positional | Falls back to `PARAPETAI_CONTROL_PLANE_URL`. |
| `agent_secret` | `None`, positional | Falls back to `PARAPETAI_AGENT_SECRET`. If, after fallback, either is still missing, raises `RuntimeError` directing you to `from_policy_dir()`. |
| `policy_dir` | required, keyword-only | Local disk-cache / initial fallback policy source. |
| `entities_path` | `None` | Optional Cedar entities file. |
| `persist_policy_dir` | `None` | Where a fetched bundle is persisted to disk. |
| `pep_key_path` | `None` | Override for where this PEP's Ed25519 keypair is stored/loaded. |
| `agent_id` | `None` | Falls back to `PARAPETAI_AGENT_ID`, then the literal `"agent"`. |
| `tenant` | `"default"` | Used when constructing the default `Caller`. |
| `mode` | `"enforce"` | Passed through to the bundle bootstrap. |
| `caller` | `None` | Overrides the default `Caller(agent_id=resolved_agent_id, tenant=tenant)`. |
| `on_decision` | `None` | Same as above. |

**On an unreachable control plane**, it degrades to the last bundle on
disk rather than refusing to start — an outage on Parapet's side must
never take a customer's agent down. With `policy_dir` empty and nothing
yet persisted, there is no policy to enforce at all, and construction
raises: fail closed.

The returned `Governor` owns a daemon poller thread; call `.stop_sync()`
to end it (tests, or a process that constructs many).

```python
gov = Governor.from_control_plane(policy_dir="./policies")
# control_plane_url / agent_secret from PARAPETAI_CONTROL_PLANE_URL / PARAPETAI_AGENT_SECRET
```

## The three checks

Each returns a [`Decision`](decision.md). All default to
`raise_on_deny=True`: a deny raises [`GovernanceDenied`](exceptions.md), a
review raises [`GovernanceReviewRequired`](exceptions.md) — pass
`raise_on_deny=False` to get the `Decision` back directly instead and
branch on `.allowed` yourself.

### `check_input()`

```python
def check_input(
    self,
    text: str,
    *,
    roles: Sequence[str] | None = None,
    claims: Mapping[str, Any] | None = None,
    model: str | None = None,
    tools: Sequence[str] | None = None,
    raise_on_deny: bool = True,
) -> Decision:
```

Pre-model guardrail: runs any configured input scanners (PII, secrets,
injection) and a Cedar `model_call` decision **before the model sees the
prompt.**

| Parameter | Meaning |
|---|---|
| `text` | The prompt to check. |
| `roles` / `claims` | Caller identity for this specific call — Cedar principal attributes. Unauthenticated if both are `None`. |
| `model` | Model name; informational, becomes part of the audit `Snapshot`. |
| `tools` | Declared tool names for this turn. |
| `raise_on_deny` | Default `True`. |

A configured content-check scanner that errors fails closed (denies) —
never treated as "scanner unavailable, allow through."

### `authorize_tool()`

```python
def authorize_tool(
    self,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    roles: Sequence[str] | None = None,
    claims: Mapping[str, Any] | None = None,
    raise_on_deny: bool = True,
) -> Decision:
```

Authorize one tool call — by name, arguments, and caller role — against
Cedar, **before it executes.** A denied call raises (default), so it
never runs.

| Parameter | Meaning |
|---|---|
| `name` | Tool name — becomes `context.tool_name` in the Cedar evaluation. |
| `arguments` | Tool call arguments — becomes `context.tool_args`. |
| `roles` / `claims` | Caller identity for this call. |
| `raise_on_deny` | Default `True`. |

Tool arguments **are** previewable in the audit trail (unlike prompt/
response content) — they're what the policy already matched on, and an
approver who can't see which call is being approved can't meaningfully
approve it.

### `check_output()`

```python
def check_output(
    self,
    response: str,
    *,
    sources: Sequence[str] | None = None,
    roles: Sequence[str] | None = None,
    claims: Mapping[str, Any] | None = None,
    model: str | None = None,
    raise_on_deny: bool = True,
) -> Decision:
```

Post-model eval: scores groundedness (against `sources`) and runs the SLM
judge if configured, then a Cedar `post`-stage decision — **before the
answer is delivered.** A scorer that errors fails closed (denies).

| Parameter | Meaning |
|---|---|
| `response` | Model output text to check. |
| `sources` | Source documents used to score groundedness. |
| `roles` / `claims` | Caller identity. |
| `model` | Model name; informational. |
| `raise_on_deny` | Default `True`. |

## Review approvals

### `request_approval()`

```python
def request_approval(
    self,
    decision: Decision,
    *,
    action: str = "",
    tool_name: str | None = None,
    args: Mapping[str, Any] | None = None,
    preview: str | None = None,
) -> tuple[str | None, str]:
```

Queue a held call for a human. Returns `(review_id, fingerprint)`.
`review_id` is `None` when there's no control plane configured, or it
couldn't be reached — neither is an error to handle: the call was already
denied locally and stays denied; there's simply nobody to ask. Called for
you automatically by the default `raise_on_deny=True` path — call it
directly only if you passed `raise_on_deny=False` and want the review
anyway.

### `wait_for_approval()`

```python
def wait_for_approval(
    self,
    held: GovernanceReviewRequired,
    *,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
) -> bool:
```

Block until a human answers the held call. `True` means approved **and**
collected — proceed exactly once. Returns `False` for every other outcome
(denied, expired, never queued, control plane unreachable, timed out), so
there's one thing to check, and `False` is always safe (the local deny
stands). Polls rather than holding a connection open — an approval takes
as long as a human takes. See [Exceptions](exceptions.md#resolving-a-held-call)
for a worked example.

## `stop_sync()`

```python
def stop_sync(self, timeout: float | None = None) -> None:
```

Stops the background bundle poller, if this `Governor` started one. A
no-op for a `Governor` built from `from_policy_dir()` — so a caller can
always call it without knowing which constructor was used.

## See also

- [Governor guide](../frameworks/governor.md) — narrative walkthrough
- [`Decision`](decision.md) — everything a check returns
- [Exceptions](exceptions.md) — `GovernanceDenied`, `GovernanceReviewRequired`
- [ADR 0009 — the approval loop](../adr/0009-approval-loop.md)
