# Per-tool credential metadata (`access_identity`)

`parapetai_agent.access_identity` — since **0.17.0**. Lets a tool declare
*which credential* it presented to reach a downstream vendor — distinct
from [`vendor_calls`](vendor-calls.md), which declares *what* the tool did
(the target system/operation), and from the agent's own identity, which is
one identity for the whole trace.

## Why this exists

An agent can have a Salesforce tool authenticating as an OAuth service
principal and an Atlassian tool authenticating with a personal access
token. Before this module, both tool calls carried the *same*
`identity_claims`/`agent_identity_claims` into `context` — only
`vendor_system` (`"salesforce"` vs. `"atlassian"`) told them apart. There
was no way for Cedar, an audit record, or an operator console to see which
credential actually reached which vendor.

`access_identity` gives you this signal in **three** ways, checked in
order, with each falling back to the next:

1. **Explicit decorator** (`@declare_access_identity`) or **framework-native
   metadata** — a tool author's own assertion. Strongest signal, and the
   only path that can name a real `type` (service account, PAT, OAuth
   service principal, ...) or a `target_endpoint`.
2. **Automatic inference** — zero extra code. If neither of the above is
   present, every integration surface synthesizes an `AccessIdentity`
   from whatever identity claims are *already ambient* at the time of the
   tool call (via `governed_identity()`/`current_identity()`, or an RFC
   8693 delegated-agent identity) plus the tool's own already-declared
   `vendor_system` ([`vendor_calls`](vendor-calls.md)). This is what makes
   the feature useful with **no per-tool work at all** for the common
   case — see "Automatic inference" below.
3. **Nothing** — `context.access_identity` is simply absent, same as
   `vendor_system` being absent for an undeclared tool. Never guessed at.

All three land on the exact same `Snapshot.access_identity` field and
`context.access_identity` key — a policy or an operator console reading it
never needs to know which of the three produced a given row (though
`source` tells you: `declared`, `inferred`, `observed`, or `verified`).

## What's actually automatic today, per framework — be precise about this

"Automatic inference" (path 2 above) only ever fills in `AccessIdentity.id`
(from whatever's ambient) and `used_to_access` (from a tool's own resolved
`vendor_system`). **`vendor_system` itself is never automatic on any
framework today** — every tool still needs its own `@declare_vendor_call`
(or framework-native metadata), because none of MAF/ADK/LangGraph/Governor
exposes a "which vendor did this call reach" field the SDK could read
without a declaration. Don't let "automatic inference" read as "zero
instrumentation" — it removes the credential-`id` declaration, not the
vendor declaration.

| Framework | `vendor_system` | credential `id`/`type` |
|---|---|---|
| Governor / MAF / LangGraph | Manual — `@declare_vendor_call` always required | Automatic `id` via inference (path 2) once `governed_identity()`/`current_identity()` is set; `type` always `UNKNOWN` unless declared explicitly |
| ADK | Manual — same as above | Same automatic inference applies today. **Not yet implemented, but the strongest candidate for a real zero-instrumentation path**: ADK's own `google.adk.auth.auth_credential.AuthCredential` (a genuinely general, typed credential model used across ADK's tool ecosystem, not MCP-specific) is already attached to any `MCPTool`/`BaseAuthenticatedTool` a developer configures for real auth — reachable as `tool._auth_config.raw_auth_credential` from inside `before_tool_callback`/`after_tool_callback`, which `adk.py` already receives `tool` in. Wiring this in would give `AccessIdentityType`/`id` for free, with no new developer-facing API, since a working authenticated ADK tool already supplies these fields for functional reasons. Track this as a planned enhancement, not a shipped one. |
| Plain HTTP tool, no MCP, no framework wrapper | Manual, always | No ambient signal exists to observe at all — a bare `requests.post(url)` inside a tool function is invisible to every mechanism above; `governed_identity(claims=...)` + `@declare_vendor_call` is the floor, permanently, for this case |

MAF's `MCPStreamableHTTPTool(header_provider=...)` is a genuine per-call
credential-injection point for the real outbound HTTP header, but it's a
one-way hook toward the wire — nothing reads its output back into Cedar's
context automatically. It DOES read the same `function_invocation_kwargs`
dict `_identity_claims()`/`_agent_identity_claims()` already read, so
sourcing one claim from that shared dict for both the real header and
`context.access_identity` is possible today, by hand — see MAF's own
module docstring for the kwargs-sharing mechanism.

## Automatic inference (no declaration needed)

The common case — you already scope identity per call with
`governed_identity()`/`current_identity()`, and you already declare
`vendor_calls` on your tools for resource scoping — needs **no new code**:

```python
from parapetai_agent.scoped_data import governed_identity
from parapetai_agent.vendor_calls import VendorCallSpec, declare_vendor_call

@declare_vendor_call(VendorCallSpec(
    vendor_system="atlassian", resource_type="Issue", crud_action="create",
))
def create_issue(project: str) -> str: ...

with governed_identity(claims=atlassian_service_creds):  # e.g. {"client_id": "..."}
    await agent.run(prompt)
```

`create_issue`'s tool call automatically gets a `context.access_identity`
of `{"id": "<the client_id from atlassian_service_creds>", "type":
"unknown", "used_to_access": "atlassian", "source": "inferred"}` — no
`@declare_access_identity`, no metadata dict, nothing tool-specific.

`infer_access_identity()` (`parapetai_agent.access_identity`) is what does
this, called by every integration surface only when neither of the two
declared paths above produced anything:

- **`id`** comes from whichever of the calling agent's own claims
  (preferred) or the ambient end-user claims (fallback) carries
  `client_id`, `azp`, `appid`, `oid`, or `sub` — checked in that order,
  the same three RFC 8693/9068 delegation-claim names
  `token_identity.agent_identity_from_claims()` already checks for the
  agent identity, plus a bare subject-identifier fallback for the common
  case of `governed_identity(claims=...)` used directly (which populates
  only end-user `Identity.claims`, never `AgentIdentity` — see that
  function's own module docstring).
- **`used_to_access`** comes from the tool's own resolved `vendor_system`
  — if a tool has no declared vendor metadata, there is nothing to infer
  `used_to_access` from, so nothing is synthesized at all, even if
  identity claims are ambient.
- **`type`** is always `UNKNOWN` — this function has no way to know
  whether a claims-derived id names a service account, a PAT, or an OAuth
  service principal. Use an explicit declaration (below) when you want a
  real `type` or a `target_endpoint`.
- **`source`** is always `INFERRED` — a distinct, weaker tier than
  `DECLARED` (nobody asserted *this specific credential*; it was
  correlated after the fact from two already-ambient facts), which an
  operator console or a policy can filter on separately.

An explicit `@declare_access_identity`/metadata declaration on the same
tool always **wins** over inference — this is a pure fallback, never an
override of something more specific.

## Declaring a tool explicitly

```python
from parapetai_agent.access_identity import (
    AccessIdentity, AccessIdentityType, declare_access_identity,
)
from parapetai_agent.vendor_calls import VendorCallSpec, declare_vendor_call

@declare_access_identity(AccessIdentity(
    id="salesforce-sa@example.iam.gserviceaccount.com",
    type=AccessIdentityType.SERVICE_ACCOUNT,
    used_to_access="salesforce",
    target_endpoint="https://mycompany.my.salesforce.com",
))
@declare_vendor_call(VendorCallSpec(
    vendor_system="salesforce", resource_type="Case", crud_action="delete",
))
def delete_salesforce_case(case_id: str) -> str: ...
```

The two decorators stack independently — a tool can carry either, both, or
neither.

`AccessIdentity` fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | `str` | An identifier for the credential — a `client_id`, key id, service-account principal, or certificate CN/fingerprint. **Never the credential's own secret value.** |
| `type` | `AccessIdentityType` | One of `SERVICE_ACCOUNT`, `PERSONAL_ACCESS_TOKEN`, `OAUTH_SERVICE_PRINCIPAL`, `API_KEY`, `STATIC_SECRET`, `MTLS`, `UNKNOWN`. |
| `used_to_access` | `str` | The vendor/system label, e.g. `"salesforce"` — same vocabulary as `VendorCallSpec.vendor_system`. |
| `target_endpoint` | `str \| None` | The URL/host this credential was presented to. Sanitize before declaring it (no query string / tokens embedded in the URL). |
| `scope` | `tuple[str, ...]` | OAuth scopes, an IAM role, or PAT scope labels, if known. |
| `source` | `AccessIdentitySource` | `DECLARED` (default — an explicit decorator/metadata assertion), `INFERRED` (this module's own automatic synthesis — see above), `OBSERVED`, or `VERIFIED`. Only the first two are produced today; the latter two are reserved for a later corroboration/control-plane-verification pass. |
| `expires_at` | `str \| None` | ISO8601, only for credential types that carry an expiry. |

The decorator attaches the identity to the **underlying Python callable**
(`func.__parapet_access_identity__`), exactly like
[`declare_vendor_call`](vendor-calls.md) — same reason: MAF/ADK/LangGraph
retain a reference back to that callable at their own interception points,
even though their wrapper types share no common base.

## Declaring a tool you don't own the source of

Same shape as `vendor_calls`' metadata path —
`resolve_access_identity_from_metadata()` reads three required `parapet_*`
keys (plus one optional) from a framework-native metadata dict:

```python
tool = FunctionTool(
    func=some_mcp_tool,
    custom_metadata={
        "parapet_access_identity_id": "salesforce-sa@example.iam.gserviceaccount.com",
        "parapet_access_identity_type": "service_account",
        "parapet_access_identity_used_to_access": "salesforce",
        "parapet_access_identity_target_endpoint": "https://mycompany.my.salesforce.com",
    },
)
```

An unrecognised `parapet_access_identity_type` value resolves to
`AccessIdentityType.UNKNOWN` rather than raising. For
[`Governor`](governor.md), pass the same dict directly on
`authorize_tool(metadata=...)`.

## Per-framework wiring

All four integration surfaces resolve access-identity metadata
automatically once a tool carries it, then fall back to
`infer_access_identity()` when it doesn't — same precedence as
`vendor_calls` for the declared half (metadata checked first, falling
back to the decorator):

- **MAF** (`ParapetFunctionMiddleware.process`): checks the decorator path
  on `context.function.func` (no native metadata dict on MAF's own tool
  wrapper, same as `vendor_calls` there), then falls back to inference
  from `_identity_claims`/`_agent_identity_claims`.
- **ADK** (`before_tool_callback`/`after_tool_callback`): checks
  `tool.custom_metadata` first, falling back to the decorator on
  `tool.func`, then to inference from the resolved `correlation.identity_claims`/
  `correlation.agent_identity_claims`.
- **LangGraph** (`ParapetAgentMiddleware._tool_snapshot`): checks
  `tool.metadata` first, falling back to the decorator on `tool.func`,
  then to inference from `_effective_identity_claims`/
  `_effective_agent_identity_claims`.
- **`Governor.authorize_tool()`**: checks its own `metadata=` argument
  first, falling back to the decorator on its own `func=` argument, then
  to inference from its own resolved `claims=`/`agent_claims=`.
  `Governor.tool()` resolves `func=f` automatically, same as it does for
  `vendor_calls`.

The resolved identity lands on `Snapshot.access_identity` and reaches
Cedar's `context.access_identity` as a nested object (`id`, `type`,
`used_to_access`, `source`, and `target_endpoint`/`scope`/`expires_at` when
set) — never stripped by `content_free()`, so it stays visible in the
content-free decision audit, same as `vendor_system`/`crud_action`.

## Writing a policy against it

```cedar
forbid(principal, action == Action::"tool_call", resource)
when {
  context has access_identity &&
  context.access_identity.source == "declared" &&
  context.access_identity.type == "static_secret"
}
unless { principal in Agent::"role:break-glass" };
```

## See also

- [Vendor/CRUD metadata](vendor-calls.md) — the companion "what did this
  tool do" declaration; `access_identity` answers "which credential did it
  use," a separate question.
- [`Decision`](decision.md) — how `context` reaches Cedar.
