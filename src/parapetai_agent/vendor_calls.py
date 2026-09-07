"""Vendor/CRUD metadata for tool calls -- declared, not observed.

None of MAF/ADK/LangGraph's interception hooks see inside a tool function's
body, only the wrapper object and call arguments. So the real downstream
vendor operation (e.g. "DELETE against Salesforce Case") can only come from
metadata attached by whoever writes the tool -- trusted but spoofable, the
same trust class a tool's own declared `name` already carries (see
auth-integrations.md finding #1). Observed corroboration from the tool's
real network call is a separate, harder-to-spoof signal layered on top later
(auth-integrations.md §7); this module is the declared half only.

Attaches to the underlying Python callable, not to any framework's wrapper
class -- this is what lets one mechanism work across MAF, ADK, and
LangGraph, since each adapter retains a reference back to that callable at
its interception point (MAF: `context.function.func`; ADK:
`tool.func` on a `FunctionTool`; LangGraph: `tool.func` on a
`StructuredTool`), even though the wrapper types themselves share no common
base across frameworks.

`resolve_vendor_call_from_metadata()` is the companion path for a tool
declared through a framework's own native metadata dict instead of this
decorator (ADK's `BaseTool.custom_metadata`, already documented upstream for
"tool manifests"; LangChain's `BaseTool.metadata`) -- useful for a tool this
codebase doesn't own the source of (e.g. a third-party MCP server exposed as
an ADK/LangChain tool), where `@declare_vendor_call` can't be applied at
definition time at all.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class VendorCallSpec:
    """What a tool function actually does against a downstream vendor.

    `crud_action` as a plain string covers a tool that only ever does one
    thing (`delete_salesforce_case` always deletes). The callable form
    covers a generic passthrough tool (e.g. `salesforce_request(method,
    path, body)`) whose real CRUD verb depends on the call's own arguments
    -- it receives the tool's resolved call arguments and must return the
    verb string itself.
    """

    vendor_system: str  # e.g. "salesforce"
    resource_type: str  # e.g. "Case"
    crud_action: str | Callable[[Mapping[str, Any]], str]  # e.g. "delete"
    operation: str | None = None  # defaults to f"{resource_type}.{crud_action}"


def declare_vendor_call(spec: VendorCallSpec) -> Callable[[F], F]:
    """Decorator attaching `spec` to the underlying callable, so
    resolve_vendor_call() can recover it later from whatever framework
    wrapper ends up holding a reference to this function.

        @declare_vendor_call(VendorCallSpec(
            vendor_system="salesforce", resource_type="Case", crud_action="delete",
        ))
        def delete_salesforce_case(case_id: str) -> str: ...
    """

    def _wrap(func: F) -> F:
        func.__parapet_vendor_call__ = spec  # type: ignore[attr-defined]
        return func

    return _wrap


def resolve_vendor_call(
    func: Callable[..., Any] | None, args: Mapping[str, Any]
) -> tuple[str, str, str] | None:
    """Returns (vendor_system, operation, crud_action) if `func` was
    decorated with @declare_vendor_call, else None. `args` is only
    consulted when `crud_action` is itself callable -- never otherwise, so
    a plain-string spec never even looks at the call's arguments."""
    spec = getattr(func, "__parapet_vendor_call__", None) if func is not None else None
    if not isinstance(spec, VendorCallSpec):
        return None
    crud = spec.crud_action(args) if callable(spec.crud_action) else spec.crud_action
    return spec.vendor_system, spec.operation or f"{spec.resource_type}.{crud}", crud


#: Keys a framework-native metadata dict (ADK's `custom_metadata`,
#: LangChain's `metadata`) must carry for resolve_vendor_call_from_metadata()
#: to recognise it. Not part of any upstream framework's own vocabulary --
#: this is this SDK's convention for a tool manifest that wants to declare
#: vendor/CRUD facts without importing this module at all (e.g. a manifest
#: authored as plain JSON/YAML for a third-party MCP tool).
VENDOR_SYSTEM_KEY = "parapet_vendor_system"
RESOURCE_TYPE_KEY = "parapet_resource_type"
CRUD_ACTION_KEY = "parapet_crud_action"
OPERATION_KEY = "parapet_operation"


def resolve_vendor_call_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> tuple[str, str, str] | None:
    """Same result shape as resolve_vendor_call(), sourced from a
    framework-native metadata dict instead of a decorator. All of
    VENDOR_SYSTEM_KEY/RESOURCE_TYPE_KEY/CRUD_ACTION_KEY must be present and
    truthy, or this returns None -- a partially-filled manifest is treated
    as undeclared, not guessed at. `crud_action` here is always a literal
    value already (a JSON/YAML manifest can't carry a callable), unlike
    VendorCallSpec.crud_action."""
    if not metadata:
        return None
    vendor_system = metadata.get(VENDOR_SYSTEM_KEY)
    resource_type = metadata.get(RESOURCE_TYPE_KEY)
    crud_action = metadata.get(CRUD_ACTION_KEY)
    if not (vendor_system and resource_type and crud_action):
        return None
    operation = metadata.get(OPERATION_KEY) or f"{resource_type}.{crud_action}"
    return str(vendor_system), str(operation), str(crud_action)
