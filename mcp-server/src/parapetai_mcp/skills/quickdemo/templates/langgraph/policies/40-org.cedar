// Local-mode copy of ../policy/40-org.cedar -- loaded straight from disk by
// example_governed.py's GovernedAgent(policy_dir="./policies", ...) when
// PARAPETAI_MODE=local (see .env.local / README.md). The cloud-mode path
// (PARAPETAI_MODE=cloud, the default) instead pulls this SAME rule from the
// real control-plane bundle it was pushed into during provisioning
// (parapet_push_policy_file) -- this file is not read in cloud mode at all.
// Kept identical to ../policy/40-org.cedar on purpose, so switching
// PARAPETAI_MODE doesn't change what's enforced, only where it's enforced
// from. Add more .cedar files to this directory (see the optional ones the
// quickdemo skill can seed here, e.g. 50-deny-salesforce.cedar) to test
// additional rules purely locally, with no control-plane bundle edit.

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
