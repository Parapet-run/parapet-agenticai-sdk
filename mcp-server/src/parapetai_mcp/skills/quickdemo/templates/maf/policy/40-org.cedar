// Org-scoped tool access, backed by real end-user identity (context.
// identity_claims -- see parapetai-agent/src/parapetai_agent/providers/parsers.py's
// Snapshot.identity_claims and parapetai_agent.scoped_data.governed_identity()).
// Same shape as policies/30-identity.cedar's role gates, one level down:
// where 30-identity.cedar gates on a role claim (a Set<String>), this
// gates on a single scalar claim, org, from a flat identity_claims dict.
//
// This file is NOT loaded from local disk by example_governed.py -- it is
// PUSHED into the provisioned agent's control-plane bundle (via
// parapet_push_policy_file / POST /api/v1/cli/agents/{agent_id}/bundle) and
// pulled back down by GovernedAgent at startup, same as any policy an
// operator edits in the console. It's kept here only so a human reading
// this example can see the exact rule that produces the DENY without
// opening the control plane.
//
// Scoped to `context has identity_claims` so this never changes behaviour
// for a caller that asserts no end-user identity at all -- only a caller
// that DOES assert an org, but the wrong one for the tool, is denied.

@id("salesforce_requires_sales_org")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "salesforce_lookup" &&
  !(context has identity_claims &&
    context.identity_claims has org &&
    context.identity_claims.org == "Sales")
};

@id("hr_system_requires_hr_org")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "hr_lookup" &&
  !(context has identity_claims &&
    context.identity_claims has org &&
    context.identity_claims.org == "HR")
};
