# Vendor/CRUD metadata (`vendor_calls`)

`parapetai_agent.vendor_calls` — since **0.7.0**. Lets a tool declare what
it actually does against a downstream vendor ("DELETE against Salesforce
Case"), so Cedar can gate on that instead of on an opaque tool name.

## Why this exists

None of MAF/ADK/LangGraph's interception hooks see inside a tool
function's body — only the wrapper object and the call arguments. Without
this module, every tool call carries only `tool_name` (a free-form,
spoofable string) into `context`, and `resource` collapses to the
*LLM provider* (`Resource::"openai"`), not the vendor the tool actually
talks to. Every Salesforce tool and every ServiceNow tool routed through
the same model provider are indistinguishable to Cedar at the resource
level.

`vendor_calls` gives you a **declared** signal — trusted but spoofable,
the same trust class a tool's own `name` already carries — layered under
by [observed corroboration](corroboration.md) (0.8.0+), which is
harder to spoof but only fires once the call actually executes.

## Declaring a tool

```python
from parapetai_agent.vendor_calls import VendorCallSpec, declare_vendor_call

@declare_vendor_call(VendorCallSpec(
    vendor_system="salesforce",
    resource_type="Case",
    crud_action="delete",
))
def delete_salesforce_case(case_id: str) -> str: ...
```

`VendorCallSpec` fields:

| Field | Type | Meaning |
|---|---|---|
| `vendor_system` | `str` | e.g. `"salesforce"`. Becomes `context.vendor_system` and the `<vendor_system>` half of the vendor-scoped Cedar resource. |
| `resource_type` | `str` | e.g. `"Case"`. Used to build the default `operation` string. |
| `crud_action` | `str \| Callable[[Mapping[str, Any]], str]` | A literal verb (`"delete"`) for a tool that only ever does one thing, or a callable that receives the tool's resolved call arguments and returns the verb — for a generic passthrough tool (e.g. `salesforce_request(method, path, body)`) whose real CRUD verb depends on the call itself. |
| `operation` | `str \| None` | Defaults to `f"{resource_type}.{crud_action}"`. |

The decorator attaches the spec to the **underlying Python callable**
(`func.__parapet_vendor_call__`), not to any framework's wrapper class —
this is what lets one mechanism work across MAF, ADK, and LangGraph, since
each adapter retains a reference back to that callable at its own
interception point (MAF: `context.function.func`; ADK: `tool.func`;
LangGraph: `tool.func` on a `StructuredTool`).

## Declaring a tool you don't own the source of

For a tool exposed through a framework's own native metadata dict instead
— e.g. a third-party MCP server surfaced as an ADK or LangChain tool,
where you can't apply `@declare_vendor_call` at definition time —
`resolve_vendor_call_from_metadata()` reads four `parapet_*`-prefixed keys
from that dict:

```python
tool = FunctionTool(
    func=some_mcp_tool,
    custom_metadata={
        "parapet_vendor_system": "salesforce",
        "parapet_resource_type": "Case",
        "parapet_crud_action": "delete",
        # "parapet_operation": "Case.delete",   # optional, same default as above
    },
)
```

ADK's `BaseTool.custom_metadata` and LangChain's `BaseTool.metadata` are
both checked automatically by the framework adapters — see
[Per-framework wiring](#per-framework-wiring) below. All four keys
(`vendor_system`/`resource_type`/`crud_action`; `operation` is optional)
must be present and truthy or the tool is treated as **undeclared**, not
guessed at. Unlike the decorator, `crud_action` here is always a literal
string — a JSON/YAML-sourced manifest can't carry a callable.

For [`Governor`](governor.md) (no framework tool object to read a native
metadata dict off of at all), `metadata=` is the *natural* path — pass it
directly on `authorize_tool()`:

```python
gov.authorize_tool(
    "salesforce_request", {"case_id": "500x"},
    metadata={
        "parapet_vendor_system": "salesforce",
        "parapet_resource_type": "Case",
        "parapet_crud_action": "delete",
    },
)
```

## Per-framework wiring

All four integration surfaces resolve vendor metadata automatically once
a tool carries it — no extra wiring beyond `@declare_vendor_call` or
`custom_metadata`/`metadata` is required:

- **MAF** (`ParapetFunctionMiddleware.process`): checks the decorator path
  on `context.function.func`.
- **ADK** (`before_tool_callback`): checks `tool.custom_metadata` first,
  falling back to the decorator on `tool.func`.
- **LangGraph** (`ParapetAgentMiddleware._tool_snapshot`): checks
  `tool.metadata` first, falling back to the decorator on `tool.func`.
- **`Governor.authorize_tool()`**: checks its own `metadata=` argument
  first, falling back to the decorator on its own `func=` argument.
  `Governor.tool()` (the decorator convenience) resolves `func=f`
  automatically — no need to also pass it to `authorize_tool()` yourself:

  ```python
  @gov.tool
  @declare_vendor_call(VendorCallSpec(
      vendor_system="salesforce", resource_type="Case", crud_action="delete",
  ))
  def delete_salesforce_case(case_id: str) -> str: ...
  ```

The resolved facts land on `Snapshot` as `vendor_system`,
`vendor_operation`, and `crud_action`, which flow into Cedar's `context`
exactly like `tool_name`/`tool_args` do — and, like those two, are
**never stripped** by `content_free()`, so they remain visible in the
content-free decision audit.

## Gateway: MCP tool mapping

`parapetai-gateway` cannot decorate a tool — it only sees a `tools/call` naming
one. Instead the operator declares what each MCP tool does, in
`PARAPETAI_MCP_TOOL_MAP` (inline JSON, or a path to a JSON file):

```json
{
  "salesforce": {
    "get_case":    {"vendor_system": "salesforce", "resource_type": "Case", "crud_action": "read"},
    "delete_case": {"vendor_system": "salesforce", "resource_type": "Case", "crud_action": "delete"},
    "sf_request": {
      "vendor_system": "salesforce", "resource_type": "Record",
      "crud_action_from": {"arg": "method",
        "map": {"GET": "read", "POST": "create", "PATCH": "update", "DELETE": "delete"}}}
  },
  "*": {"create_issue": {"vendor_system": "atlassian", "resource_type": "Issue", "crud_action": "create"}}
}
```

The first level is the MCP **target** — the `salesforce` in
`/a/<agent>/mcp/salesforce`. `"*"` matches every target, including a bare
`/mcp`; a named target wins over `"*"`. The fields are the same as
`VendorCallSpec`, and land on the same `Snapshot` fields, so a policy written
for the in-process SDK works unchanged.

- **The operator declares it, not the caller.** The lookup keys are the tool
  name and the URL's target. A tool argument called `crud_action` cannot
  override the classification (tested). That makes this a stronger signal than
  the in-process decorator, whose author is the tool's own author. It is still
  *declared*, not observed.
- **A generic tool** (`sf_request(method, path)`) has no single verb.
  `crud_action_from` derives it from one top-level string argument, compared
  case-insensitively. A value not in `map`, or no such argument, resolves to
  `crud_action == "unknown"`, **not** to "unmapped": the vendor is still known,
  and a rule like `permit … when { context.crud_action == "read" }` correctly
  refuses it, where dropping the facts would let an unrecognised verb slip past
  a `forbid … == "delete"`.
- `crud_action` is a lower-case word (`read`, `create`, `update`, `delete`,
  `admin` by convention; `unknown` is reserved). Unknown fields, both a literal
  and a derived verb, or a bad shape all stop the gateway at startup, so a typo
  cannot leave a tool silently unclassified.
- A tool with no entry is **undeclared**: no vendor context at all, and with
  `PARAPETAI_VENDOR_SCOPED_RESOURCES=true` its resource is
  `Resource::"undeclared"`, which a fail-closed deployment can forbid.

The mapping alone changes no resource, so adding it cannot break an existing
policy. Turn on `PARAPETAI_VENDOR_SCOPED_RESOURCES` later, once your policies
target `Resource::"<vendor_system>/<operation>"`.

## Writing a policy against it

No resource change is required to use the declared facts — they're
available in `context` immediately:

```cedar
forbid(principal, action == Action::"tool_call", resource)
when { context has crud_action && context.crud_action == "delete" }
unless { principal in Agent::"role:crm-admin" };
```

## `vendor_scoped_resources` — opt-in resource construction

By default, `GovernanceHook.evaluate()` still builds `resource` from the
*LLM provider* only (`Resource::"openai"`, etc.) — unchanged, so every
existing bundle keeps working with zero migration. Passing
`vendor_scoped_resources=True` to `build_middleware()` / `build_plugin()`
(MAF/ADK) or constructing `ParapetAgentMiddleware`/`GovernanceHook`
directly with it (LangGraph / `Governor`) switches `resource` to:

- `Resource::"<vendor_system>/<vendor_operation>"` — when the tool
  declared vendor metadata.
- `Resource::"undeclared"` — when it didn't. This is a **distinct**
  resource from the provider fallback, deliberately: without it, a
  broad pre-existing rule like `permit(..., resource ==
  Resource::"openai")` would silently cover an unclassified tool too,
  once vendor-scoped policies exist elsewhere. A fail-closed deployment
  can `forbid(resource == Resource::"undeclared")` explicitly.
- Unchanged (`Resource::"<provider>"`) for a genuine `model_call` — this
  flag only affects `tool_call` resource construction.

```python
from parapetai_agent.maf import build_middleware

chat_mw, func_mw = build_middleware(
    policy_dir="./policies",
    vendor_scoped_resources=True,
)
```

`GovernedAgent`, `GovernedRunner`, and `Governor.from_policy_dir()` all
accept the same `vendor_scoped_resources=` keyword directly — it's
forwarded to `build_middleware()`/`build_plugin()`/`GovernanceHook`
respectively, so there's no lower-level function you need to reach for
just to set this locally, without a control plane.

### Control-plane-driven

`Governor.from_control_plane()` / `bootstrap_engine()`'s returned
`Bootstrap` carries `vendor_scoped_resources: bool`, resolved from the
bundle response's own `vendor_scoped_resources` field when a control
plane is configured — lets an operator turn the flag on for a tenant
without a code change on the PEP side. Resolved **once, at bootstrap**
(process start) — unlike policy/entities, it does not hot-reload
mid-process on a later bundle poll, because by the time the poller is
running, `GovernanceHook` has already been constructed from this value
and there's no live reference to update. A tenant-level change on the
control plane takes effect on this PEP's next full bootstrap (process
restart), not mid-process.

## See also

- [Corroboration](corroboration.md) — the observed, harder-to-spoof
  signal layered on top of this declared one.
- [Automatic vendor/resource/permission detection](vendor-scope-permission.md) —
  the control-plane-classified successor to hand-declaring this; this
  decorator still works as an optional override signal.
- [`Decision`](decision.md) — how `context` reaches Cedar.
- [Governor](governor.md) — `from_control_plane()`.
