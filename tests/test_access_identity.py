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
    infer_access_identity,
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
    assert resolve_access_identity_from_metadata({"parapet_access_identity_id": "x"}) is None


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


class TestInferAccessIdentity:
    """The automatic path -- no decorator, no metadata. Covers the real
    motivating scenario: a caller that sets ambient identity via
    scoped_data.governed_identity(claims=...) around a whole agent run
    (which populates ONLY end-user Identity.claims, never AgentIdentity --
    see the module's own _ID_CLAIM_KEYS comment) and never wrote any
    per-tool declaration at all."""

    def test_returns_none_with_no_vendor_system(self) -> None:
        assert (
            infer_access_identity(
                agent_claims={"client_id": "x"}, identity_claims=None, vendor_system=None
            )
            is None
        )

    def test_returns_none_with_no_claims_at_all(self) -> None:
        assert (
            infer_access_identity(
                agent_claims=None, identity_claims=None, vendor_system="salesforce"
            )
            is None
        )

    def test_infers_from_agent_claims_client_id(self) -> None:
        resolved = infer_access_identity(
            agent_claims={"client_id": "salesforce-client-id"},
            identity_claims=None,
            vendor_system="salesforce",
        )
        assert resolved is not None
        assert resolved.id == "salesforce-client-id"
        assert resolved.used_to_access == "salesforce"
        assert resolved.type == AccessIdentityType.UNKNOWN
        assert resolved.source == AccessIdentitySource.INFERRED

    def test_agent_claims_win_over_identity_claims_when_both_present(self) -> None:
        resolved = infer_access_identity(
            agent_claims={"client_id": "agent-cred"},
            identity_claims={"client_id": "end-user-cred"},
            vendor_system="salesforce",
        )
        assert resolved is not None
        assert resolved.id == "agent-cred"

    def test_falls_back_to_identity_claims_when_agent_claims_empty(self) -> None:
        """The literal motivating scenario: governed_identity(claims=...)
        populates end-user Identity.claims only -- AgentIdentity stays
        empty entirely."""
        resolved = infer_access_identity(
            agent_claims={},
            identity_claims={"client_id": "atlassian-pat-owner-client-id"},
            vendor_system="atlassian",
        )
        assert resolved is not None
        assert resolved.id == "atlassian-pat-owner-client-id"
        assert resolved.used_to_access == "atlassian"

    def test_claim_key_priority_prefers_client_id_over_sub(self) -> None:
        resolved = infer_access_identity(
            agent_claims=None,
            identity_claims={"sub": "subject-x", "client_id": "client-y"},
            vendor_system="github",
        )
        assert resolved is not None
        assert resolved.id == "client-y"

    def test_falls_back_to_sub_when_no_client_id_style_claim_present(self) -> None:
        resolved = infer_access_identity(
            agent_claims=None,
            identity_claims={"sub": "subject-x"},
            vendor_system="github",
        )
        assert resolved is not None
        assert resolved.id == "subject-x"

    def test_returns_none_when_claims_present_but_no_recognised_key(self) -> None:
        resolved = infer_access_identity(
            agent_claims={"email": "alice@acme.com"},
            identity_claims=None,
            vendor_system="salesforce",
        )
        assert resolved is None


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
