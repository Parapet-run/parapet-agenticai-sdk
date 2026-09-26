---
name: parapet-identity
description: Use when the user asks to track which vendor/downstream credential each tool call uses on an ALREADY-governed project -- "add vendor tracking to my tools", "which credential is each tool using", "instrument access_identity", "show vendor per tool call in the audit trail". Requires parapetai-agent's governance already wired in (GovernedAgent/GovernedRunner/ParapetAgentMiddleware/Governor already present -- if not, run parapet-maf/parapet-adk/parapet-langgraph first). Edits tool definitions to add @declare_vendor_call (parapetai_agent.vendor_calls) and, only when the user actually wants a real credential type/endpoint recorded, @declare_access_identity (parapetai_agent.access_identity) -- a lighter, per-tool follow-up to the main governance wiring, not a replacement for it.
---

# Parapet identity/vendor instrumentation

Two separate, independent facts can be declared on a tool, each answering
a different question:

- **`vendor_system`** (`parapetai_agent.vendor_calls`) -- *what* the tool
  does ("DELETE against Salesforce Case"). Drives Cedar's vendor-scoped
  resource (`Resource::"<vendor>/<operation>"`) and shows up in the audit
  trail/operator console grouped by vendor instead of a generic model
  provider.
- **`access_identity`** (`parapetai_agent.access_identity`) -- *which
  credential* reached that vendor (a Salesforce OAuth service principal
  vs. an Atlassian personal access token, say). Distinct from the calling
  agent's own identity, which is one identity for the whole trace, not
  per tool.

Full reference: `docs/reference/vendor-calls.md` and
`docs/reference/access-identity.md` in the `parapetai-agent` repo (or the
published docs site) -- read these if anything below is ambiguous for the
target project's actual shape, since this skill summarizes them rather
than replacing them.

## 1. Confirm governance is already wired

This skill assumes tool calls are already authorized through
`GovernedAgent`/`GovernedRunner`/`ParapetAgentMiddleware`/`Governor` --
grep for one of those before doing anything else. If none is present,
stop and tell the user to run **parapet-maf**/**parapet-adk**/
**parapet-langgraph** first: adding vendor/identity metadata to an
ungoverned tool call has nothing to attach to.

## 2. Find every tool definition

Grep by framework:

- MAF: `@tool` decorator, or `FunctionTool(func=...)` construction.
- ADK: `FunctionTool(func=...)`, `MCPTool(...)`.
- LangGraph/LangChain: `@tool`, `StructuredTool.from_function(...)`.
- `Governor`: `@gov.tool`, or direct `gov.authorize_tool(...)` call sites.

For each, read the function body/name/docstring and judge whether it
clearly reaches **one identifiable downstream vendor** (an HTTP call to a
named host, a client library import like `simple_salesforce`, a name like
`delete_salesforce_case`). **Ask the user to confirm the vendor name for
anything not obvious from the code — never guess one from a vague
function name** (e.g. `send_request`, `call_api`).

## 3. Add `@declare_vendor_call`

```python
from parapetai_agent.vendor_calls import VendorCallSpec, declare_vendor_call

@declare_vendor_call(VendorCallSpec(
    vendor_system="salesforce", resource_type="Case", crud_action="delete",
))
def delete_salesforce_case(case_id: str) -> str: ...
```

`crud_action` is a lower-case verb by convention (`read`/`create`/
`update`/`delete`/`admin`). A generic passthrough tool whose real verb
depends on its own call arguments (e.g. `salesforce_request(method, path,
body)`) needs the **callable** form instead of a literal string — see
`docs/reference/vendor-calls.md`'s own example before improvising one.

For a tool you don't own the source of (a third-party MCP tool, or one
exposed only through a framework-native metadata dict), use the four
`parapet_vendor_system`/`parapet_resource_type`/`parapet_crud_action`/
`parapet_operation` metadata keys on that object instead of the
decorator — same doc, "Declaring a tool you don't own the source of."

## 4. Decide whether `access_identity` needs an explicit declaration at all

**Usually it doesn't — say this plainly before adding more code.** Once
step 3's `vendor_system` is in place, and the project already wraps tool
calls in `governed_identity(claims=...)`/`current_identity(...)` (grep
for either), `context.access_identity` is populated **automatically**
(`parapetai_agent.access_identity.infer_access_identity()`, shipped in
`parapetai-agent` 0.17+) with zero additional code. It fills in a
credential `id` (from whatever's ambient) and `used_to_access` (from the
`vendor_system` you just declared) — but `type` always resolves to
`unknown` this way, since nothing can safely guess whether an id names a
service account, a PAT, or an OAuth principal.

Only add an explicit `@declare_access_identity` when the user actually
wants a **real** `type` recorded, or a `target_endpoint`:

```python
from parapetai_agent.access_identity import (
    AccessIdentity, AccessIdentityType, declare_access_identity,
)

@declare_access_identity(AccessIdentity(
    id="salesforce-sa@example.iam.gserviceaccount.com",
    type=AccessIdentityType.SERVICE_ACCOUNT,
    used_to_access="salesforce",
    target_endpoint="https://mycompany.my.salesforce.com",
))
```

Ask which the user wants (automatic `unknown`-typed inference vs. an
explicit, correctly-typed declaration) rather than adding this decorator
to every tool reflexively — it's extra maintenance surface for
information the automatic path may already cover well enough.

## 5. Per-framework caveats — read before saying "done"

None of the frameworks derive `vendor_system` automatically, on any
version as of this writing. Be precise with the user about what's
automatic and what isn't (this table also lives in
`docs/reference/access-identity.md` — if it's changed there, trust the
doc over this skill's own copy):

| Framework | `vendor_system` | credential `id`/`type` |
|---|---|---|
| Governor / MAF / LangGraph | Manual, always | Automatic `id` once `governed_identity()`/`current_identity()` is set; `type` stays `unknown` unless declared |
| ADK | Manual, always | Same automatic inference as above applies **today**. Do not tell the user ADK reads its own native credential object (`google.adk.auth.auth_credential.AuthCredential`, attached to any `MCPTool`/`BaseAuthenticatedTool` a developer already configures for real auth) automatically — `parapetai_agent.adk` does not do this yet. Verify before claiming otherwise: `grep -n "AuthCredential\|_auth_config" $(python -c "import parapetai_agent.adk as m; print(m.__file__)")` — no output means still on the manual/inferred path like every other framework. |
| A plain tool with no MCP session and no framework wrapper (a bare function making an HTTP call) | Manual, always | Nothing is ever automatic here — no ambient signal exists to observe. `governed_identity()`/`current_identity()` plus `@declare_vendor_call` is the permanent floor for this case. |

## 6. Verify, don't just trust the edit

After editing, confirm the declarations actually resolve — same
"verify at runtime, not just from the diff" discipline `parapet-maf`'s
own instrumentation step requires:

```python
from parapetai_agent.vendor_calls import resolve_vendor_call
from parapetai_agent.access_identity import resolve_access_identity

assert resolve_vendor_call(delete_salesforce_case, {}) is not None
```

## Non-negotiables

- **Never invent a vendor name, `crud_action`, or credential `type` the
  user hasn't confirmed.** A wrong-but-plausible `vendor_system` is worse
  than none — it silently miscategorizes that tool's calls in every
  Cedar policy and audit report reading it afterward, and looks correct
  at a glance.
- **Never fabricate a `target_endpoint` or credential `id`.** Leave it
  unset rather than guess a plausible-looking value.
- **Don't add `@declare_access_identity` to every tool reflexively** once
  `@declare_vendor_call` is in place — check step 4 first.
- **Don't claim any framework "automatically" derives `vendor_system` or
  a typed credential.** As of writing, none does. If the installed
  `parapetai-agent` version is newer than what this skill describes,
  trust the installed package's own `docs/reference/access-identity.md`
  over this skill's copy of the same table.
- If the project has no `governed_identity()`/`current_identity()`
  wrapping anywhere, tell the user `access_identity` still works via an
  **explicit** declaration (step 3 + step 4's decorator), just not
  automatically — don't add a `governed_identity()` wrap around code you
  weren't asked to change.
