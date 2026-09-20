// Role-gated tool access, backed by real IdP/OIDC/JWT role claims
// (context.identity_roles, a Cedar Set<String> -- see
// parapetai-agent/src/parapetai_agent/providers/parsers.py's Snapshot.identity_roles
// and parapetai-agent/src/parapetai_agent/maf.py's _identity_roles()). The in-process
// adapters populate this from governed_identity(); the gateway populates it from a
// verified IdP token when PARAPETAI_IDP_* is configured
// (docs/reference/gateway-identity.md). An unverified HTTP-path request has no
// identity source, so it asserts none.
//
// Scoped to `context has identity_roles` so this never changes behaviour
// for a caller that doesn't assert identity at all (the HTTP path, or an
// in-process caller that never passes identity_roles) -- only a caller
// that DOES assert roles, but lacks the required one, is denied. Without
// this guard, this rule would silently start denying lookup_order for
// every existing HTTP-path caller, since context.identity_roles is never
// present there.

@id("lookup_order_requires_order_viewer_role")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "lookup_order" &&
  context has identity_roles &&
  !context.identity_roles.contains("OrderViewer")
};

// ServiceNow demo, per end-user role: reading an incident requires the
// IncidentViewer role. Same guard as above -- a caller that never asserts
// roles (the web demo's default) is unaffected; a caller that asserts roles
// but lacks IncidentViewer is denied.
@id("lookup_incident_requires_role")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "lookup_incident" &&
  context has identity_roles &&
  !context.identity_roles.contains("IncidentViewer")
};
