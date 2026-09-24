"""parapetai_agent.access_identity -- declared per-tool credential metadata.
Covers the decorator path (declare_access_identity/resolve_access_identity),
the framework-native-metadata path (resolve_access_identity_from_metadata),
and that Snapshot.to_context() surfaces the resolved identity under the
expected key. Mirrors tests/test_vendor_calls.py's own structure.
"""

from __future__ import annotations

from parapetai_agent.access_identity import (
    AccessIdentity,
    AccessIdentitySource,
    AccessIdentityType,
    declare_access_identity,
    resolve_access_identity,
    resolve_access_identity_from_metadata,
)
from parapetai_agent.providers.parsers import Snapshot


def test_resolve_access_identity_returns_none_for_an_undecorated_function() -> None:
    def plain(x: int) -> int:
        return x

    assert resolve_access_identity(plain) is None


def test_resolve_access_identity_returns_none_for_none() -> None:
    assert resolve_access_identity(None) is None


def test_declare_access_identity() -> None:
    @declare_access_identity(
        AccessIdentity(
            id="salesforce-sa@example.iam",
            type=AccessIdentityType.SERVICE_ACCOUNT,
            used_to_access="salesforce",
            target_endpoint="https://mycompany.my.salesforce.com",
        )
    )
    def delete_salesforce_case(case_id: str) -> str:
        return case_id

    resolved = resolve_access_identity(delete_salesforce_case)
    assert resolved is not None
    assert resolved.id == "salesforce-sa@example.iam"
    assert resolved.type == AccessIdentityType.SERVICE_ACCOUNT
    assert resolved.used_to_access == "salesforce"
    assert resolved.target_endpoint == "https://mycompany.my.salesforce.com"
    assert resolved.source == AccessIdentitySource.DECLARED


def test_declare_access_identity_stacks_with_declare_vendor_call() -> None:
    """auth-integrations-style motivating example: a tool can carry both
    the "what it does" (vendor_calls) and "which credential" (this module)
    declarations independently."""
    from parapetai_agent.vendor_calls import (
        VendorCallSpec,
        declare_vendor_call,
        resolve_vendor_call,
    )

    @declare_access_identity(
        AccessIdentity(
            id="atlassian-pat-abc123",
            type=AccessIdentityType.PERSONAL_ACCESS_TOKEN,
            used_to_access="atlassian",
        )
    )
    @declare_vendor_call(
        VendorCallSpec(vendor_system="atlassian", resource_type="Issue", crud_action="create")
    )
    def create_issue(project: str) -> str:
        return project

    access = resolve_access_identity(create_issue)
    vendor = resolve_vendor_call(create_issue, {"project": "PROJ"})
    assert access is not None and access.type == AccessIdentityType.PERSONAL_ACCESS_TOKEN
    assert vendor == ("atlassian", "Issue.create", "create")


def test_resolve_access_identity_from_metadata_requires_all_three_keys() -> None:
    assert resolve_access_identity_from_metadata(None) is None
    assert resolve_access_identity_from_metadata({}) is None
    assert (
        resolve_access_identity_from_metadata({"parapet_access_identity_id": "x"}) is None
    )


def test_resolve_access_identity_from_metadata_resolves_when_complete() -> None:
    metadata = {
        "parapet_access_identity_id": "salesforce-sa@example.iam",
        "parapet_access_identity_type": "service_account",
        "parapet_access_identity_used_to_access": "salesforce",
        "parapet_access_identity_target_endpoint": "https://mycompany.my.salesforce.com",
    }
    resolved = resolve_access_identity_from_metadata(metadata)
    assert resolved is not None
    assert resolved.id == "salesforce-sa@example.iam"
    assert resolved.type == AccessIdentityType.SERVICE_ACCOUNT
    assert resolved.used_to_access == "salesforce"
    assert resolved.target_endpoint == "https://mycompany.my.salesforce.com"


def test_resolve_access_identity_from_metadata_unknown_type_does_not_raise() -> None:
    metadata = {
        "parapet_access_identity_id": "abc",
        "parapet_access_identity_type": "not_a_real_type",
        "parapet_access_identity_used_to_access": "github",
    }
    resolved = resolve_access_identity_from_metadata(metadata)
    assert resolved is not None
    assert resolved.type == AccessIdentityType.UNKNOWN


def test_snapshot_to_context_includes_access_identity_only_when_set() -> None:
    bare = Snapshot(provider="openai", endpoint="in-process:test:tool_call", parsed=True)
    assert "access_identity" not in bare.to_context()

    declared = Snapshot(
        provider="openai",
        endpoint="in-process:test:tool_call",
        parsed=True,
        tool_name="delete_salesforce_case",
        access_identity=AccessIdentity(
            id="salesforce-sa@example.iam",
            type=AccessIdentityType.SERVICE_ACCOUNT,
            used_to_access="salesforce",
        ),
    )
    ctx = declared.to_context()
    assert ctx["access_identity"] == {
        "id": "salesforce-sa@example.iam",
        "type": "service_account",
        "used_to_access": "salesforce",
        "source": "declared",
    }
