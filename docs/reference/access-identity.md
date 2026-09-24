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

`access_identity` gives you a **declared** signal — the same trust class as
`vendor_calls` (trusted but spoofable, not verified against the real
outbound call): a `context.access_identity` object naming an identifier for
the credential (never the credential's own secret value), its type, which
vendor it was used against, and the endpoint it was presented to.

## Declaring a tool

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
| `source` | `AccessIdentitySource` | `DECLARED` (default — this module's own trust class), `OBSERVED`, or `VERIFIED`. This module only ever produces `DECLARED`; the other two are reserved for a later corroboration/control-plane-verification pass. |
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
automatically once a tool carries it — same precedence as `vendor_calls`
(metadata checked first, falling back to the decorator):

- **MAF** (`ParapetFunctionMiddleware.process`): checks the decorator path
  on `context.function.func` (no native metadata dict on MAF's own tool
  wrapper, same as `vendor_calls` there).
- **ADK** (`before_tool_callback`/`after_tool_callback`): checks
  `tool.custom_metadata` first, falling back to the decorator on
  `tool.func`.
- **LangGraph** (`ParapetAgentMiddleware._tool_snapshot`): checks
  `tool.metadata` first, falling back to the decorator on `tool.func`.
- **`Governor.authorize_tool()`**: checks its own `metadata=` argument
  first, falling back to the decorator on its own `func=` argument.
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
