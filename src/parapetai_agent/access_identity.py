"""Per-tool-call credential metadata -- declared, not observed.

`parapetai_agent.scoped_data` already carries two identities through every
governed decision: the end user (`Identity`) and the calling agent
(`AgentIdentity`) -- see that module's own docstring, "two identities, not
one." Neither answers a third, distinct question: *which downstream
credential did THIS tool call actually present*? An agent with both a
Salesforce tool (authenticating as an OAuth service principal) and an
Atlassian tool (authenticating with a personal access token) has one agent
identity and one end-user identity for the whole trace, but two different
credentials reaching two different vendors -- today's context can tell
Cedar "vendor_system=salesforce" (parapetai_agent.vendor_calls) but not
"reached via client_id=xyz, a service_account."

This module is the declared half of that third dimension, deliberately
mirroring vendor_calls.py's own shape and its own trust framing: attaches to
the underlying Python callable (not a framework wrapper class), resolved the
same two ways (`@declare_access_identity` decorator, or a framework-native
metadata dict via `resolve_access_identity_from_metadata`), and is exactly
as trustworthy as a tool's own declared name -- trusted but spoofable, not
verified against the real outbound call. Observed corroboration
(parapetai_agent.corroboration already captures the real outbound span; a
later change can promote a declared AccessIdentity to "observed" by
matching transport-layer evidence against it) is separate, harder-to-spoof
work layered on top later, not part of this module.

No `AccessIdentity` ever carries raw secret material (a token, key, or
password) -- only an identifier for the credential (a client_id, key id,
service-account principal, or certificate CN/fingerprint). A caller that
would otherwise pass a bare secret string here is misusing this module; it
exists to fingerprint which credential was used, never to store it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class AccessIdentityType(StrEnum):
    """Closed vocabulary -- Cedar policy needs a fixed set of strings to
    reason over (e.g. `forbid` any credential of the static-secret type
    from reaching a sensitive resource), not a free-form label a tool
    author could spell a dozen different ways."""

    SERVICE_ACCOUNT = "service_account"
    PERSONAL_ACCESS_TOKEN = "personal_access_token"  # noqa: S105 -- a type-name label, not a token value
    OAUTH_SERVICE_PRINCIPAL = "oauth_service_principal"
    API_KEY = "api_key"
    STATIC_SECRET = "static_secret"  # noqa: S105 -- a type-name label, not a credential value
    MTLS = "mtls"
    UNKNOWN = "unknown"  # fail-closed default -- never silently coerced to a known type


class AccessIdentitySource(StrEnum):
    """How confident this AccessIdentity is. DECLARED is this module's own
    trust class (see module docstring); OBSERVED/VERIFIED describe stronger
    evidence a later change can promote a DECLARED identity to, once
    corroboration.py or a control-plane credential inventory can confirm
    it -- neither is produced by this module today."""

    DECLARED = "declared"
    OBSERVED = "observed"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class AccessIdentity:
    """The credential a tool call presented to reach one specific
    downstream system -- distinct from `AgentIdentity` (scoped_data.py),
    which is the calling agent's own identity for the whole trace, not a
    per-tool credential.

    `id` is an identifier for the credential (client_id, key id,
    service-account principal, certificate CN/fingerprint), never the
    credential's own secret value -- see module docstring.
    """

    id: str
    type: AccessIdentityType
    # vendor/system label, e.g. "salesforce" -- same vocabulary as
    # vendor_calls.VendorCallSpec.vendor_system
    used_to_access: str
    # the URL/host this credential was presented to, e.g.
    # "https://mycompany.my.salesforce.com" -- sanitized (no query string)
    # before storage, same content-free discipline as everything else audited
    target_endpoint: str | None = None
    scope: tuple[str, ...] = ()
    source: AccessIdentitySource = AccessIdentitySource.DECLARED
    expires_at: str | None = None  # ISO8601, only for types with an expiry (OAuth token, cert)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "used_to_access": self.used_to_access,
            "source": self.source.value,
        }
        if self.target_endpoint:
            d["target_endpoint"] = self.target_endpoint
        if self.scope:
            d["scope"] = list(self.scope)
        if self.expires_at:
            d["expires_at"] = self.expires_at
        return d


def declare_access_identity(identity: AccessIdentity) -> Callable[[F], F]:
    """Decorator attaching `identity` to the underlying callable, so
    resolve_access_identity() can recover it later -- same mechanism as
    vendor_calls.declare_vendor_call, and stackable with it:

        @declare_access_identity(AccessIdentity(
            id="salesforce-sa@example.iam", type=AccessIdentityType.SERVICE_ACCOUNT,
            used_to_access="salesforce", target_endpoint="https://mycompany.my.salesforce.com",
        ))
        @declare_vendor_call(VendorCallSpec(
            vendor_system="salesforce", resource_type="Case", crud_action="delete",
        ))
        def delete_salesforce_case(case_id: str) -> str: ...
    """

    def _wrap(func: F) -> F:
        func.__parapet_access_identity__ = identity  # type: ignore[attr-defined]
        return func

    return _wrap


def resolve_access_identity(func: Callable[..., Any] | None) -> AccessIdentity | None:
    """Returns the AccessIdentity if `func` was decorated with
    @declare_access_identity, else None."""
    identity = getattr(func, "__parapet_access_identity__", None) if func is not None else None
    return identity if isinstance(identity, AccessIdentity) else None


#: Keys a framework-native metadata dict must carry for
#: resolve_access_identity_from_metadata() to recognise it -- same
#: convention as vendor_calls.py's own VENDOR_SYSTEM_KEY etc, for a tool
#: this codebase doesn't own the source of.
ACCESS_IDENTITY_ID_KEY = "parapet_access_identity_id"
ACCESS_IDENTITY_TYPE_KEY = "parapet_access_identity_type"
ACCESS_IDENTITY_USED_TO_ACCESS_KEY = "parapet_access_identity_used_to_access"
ACCESS_IDENTITY_TARGET_ENDPOINT_KEY = "parapet_access_identity_target_endpoint"


def resolve_access_identity_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> AccessIdentity | None:
    """Same result shape as resolve_access_identity(), sourced from a
    framework-native metadata dict. All of ACCESS_IDENTITY_ID_KEY/
    ACCESS_IDENTITY_TYPE_KEY/ACCESS_IDENTITY_USED_TO_ACCESS_KEY must be
    present and truthy, or this returns None -- a partially-filled manifest
    is treated as undeclared, not guessed at. An unrecognised type string
    resolves to AccessIdentityType.UNKNOWN rather than raising -- fail
    closed on the CONTENT (never claim a type that wasn't really asserted),
    not on the whole call."""
    if not metadata:
        return None
    identity_id = metadata.get(ACCESS_IDENTITY_ID_KEY)
    type_str = metadata.get(ACCESS_IDENTITY_TYPE_KEY)
    used_to_access = metadata.get(ACCESS_IDENTITY_USED_TO_ACCESS_KEY)
    if not (identity_id and type_str and used_to_access):
        return None
    try:
        identity_type = AccessIdentityType(str(type_str))
    except ValueError:
        identity_type = AccessIdentityType.UNKNOWN
    return AccessIdentity(
        id=str(identity_id),
        type=identity_type,
        used_to_access=str(used_to_access),
        target_endpoint=metadata.get(ACCESS_IDENTITY_TARGET_ENDPOINT_KEY),
    )
