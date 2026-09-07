"""parapetai_agent.vendor_calls -- declared vendor/CRUD metadata for tool
calls (auth-integrations.md §2). Covers the decorator path
(declare_vendor_call/resolve_vendor_call), the framework-native-metadata
path (resolve_vendor_call_from_metadata), and that Snapshot.to_context()
surfaces the resolved facts under the expected keys.
"""

from __future__ import annotations

from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.vendor_calls import (
    VendorCallSpec,
    declare_vendor_call,
    resolve_vendor_call,
    resolve_vendor_call_from_metadata,
)


def test_resolve_vendor_call_returns_none_for_an_undecorated_function() -> None:
    def plain(x: int) -> int:
        return x

    assert resolve_vendor_call(plain, {}) is None


def test_resolve_vendor_call_returns_none_for_none() -> None:
    assert resolve_vendor_call(None, {}) is None


def test_declare_vendor_call_with_a_literal_crud_action() -> None:
    @declare_vendor_call(
        VendorCallSpec(vendor_system="salesforce", resource_type="Case", crud_action="delete")
    )
    def delete_salesforce_case(case_id: str) -> str:
        return case_id

    resolved = resolve_vendor_call(delete_salesforce_case, {"case_id": "500x"})
    assert resolved == ("salesforce", "Case.delete", "delete")


def test_declare_vendor_call_with_an_explicit_operation() -> None:
    @declare_vendor_call(
        VendorCallSpec(
            vendor_system="servicenow",
            resource_type="Incident",
            crud_action="update",
            operation="Incident.close",
        )
    )
    def close_incident(incident_id: str) -> str:
        return incident_id

    resolved = resolve_vendor_call(close_incident, {"incident_id": "INC-42"})
    assert resolved == ("servicenow", "Incident.close", "update")


def test_declare_vendor_call_with_a_callable_crud_action_resolved_from_args() -> None:
    """Covers a generic passthrough tool whose real CRUD verb depends on
    the call's own arguments (§2's `salesforce_request(method, path,
    body)` example)."""

    def crud_from_method(args: dict) -> str:
        return {"GET": "read", "POST": "create", "DELETE": "delete"}[args["method"]]

    @declare_vendor_call(
        VendorCallSpec(
            vendor_system="salesforce", resource_type="Generic", crud_action=crud_from_method
        )
    )
    def salesforce_request(method: str, path: str) -> str:
        return path

    resolved = resolve_vendor_call(salesforce_request, {"method": "DELETE", "path": "/Case/1"})
    assert resolved == ("salesforce", "Generic.delete", "delete")


def test_resolve_vendor_call_from_metadata_requires_all_three_keys() -> None:
    assert resolve_vendor_call_from_metadata(None) is None
    assert resolve_vendor_call_from_metadata({}) is None
    assert resolve_vendor_call_from_metadata({"parapet_vendor_system": "github"}) is None


def test_resolve_vendor_call_from_metadata_resolves_when_complete() -> None:
    metadata = {
        "parapet_vendor_system": "github",
        "parapet_resource_type": "Repository",
        "parapet_crud_action": "delete",
    }
    assert resolve_vendor_call_from_metadata(metadata) == ("github", "Repository.delete", "delete")


def test_resolve_vendor_call_from_metadata_honors_explicit_operation() -> None:
    metadata = {
        "parapet_vendor_system": "github",
        "parapet_resource_type": "Repository",
        "parapet_crud_action": "delete",
        "parapet_operation": "GITHUB_DELETE_A_REPOSITORY",
    }
    resolved = resolve_vendor_call_from_metadata(metadata)
    assert resolved == ("github", "GITHUB_DELETE_A_REPOSITORY", "delete")


def test_snapshot_to_context_includes_vendor_fields_only_when_set() -> None:
    bare = Snapshot(provider="openai", endpoint="in-process:test:tool_call", parsed=True)
    assert "vendor_system" not in bare.to_context()

    declared = Snapshot(
        provider="openai",
        endpoint="in-process:test:tool_call",
        parsed=True,
        tool_name="delete_salesforce_case",
        vendor_system="salesforce",
        vendor_operation="Case.delete",
        crud_action="delete",
    )
    ctx = declared.to_context()
    assert ctx["vendor_system"] == "salesforce"
    assert ctx["vendor_operation"] == "Case.delete"
    assert ctx["crud_action"] == "delete"
