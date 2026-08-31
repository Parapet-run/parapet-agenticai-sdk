// OPTIONAL local test policy -- not pushed to the control plane, only
// loaded when PARAPETAI_MODE=local (see example_governed.py / README.md).
//
// Layers a second requirement on top of 40-org.cedar's org gate: hr_lookup
// also needs an explicit role claim of "HRManager", not just org == "HR".
// The demo's governed_identity() calls only assert {org, name} (see
// SCENARIOS in example_governed.py), so with this policy applied Sally's
// hr_lookup call flips from ALLOWED to DENIED too -- demonstrating that
// forbid rules compose (Cedar has no rule-priority/override concept; every
// determining forbid must fail to match for a permit to stand). To see
// Sally allowed again under this policy, add a "role" claim to her
// governed_identity() call in example_governed.py and re-run.

@id("hr_lookup_requires_hr_manager_role")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "hr_lookup" &&
  !(context has identity_claims &&
    context.identity_claims has role &&
    context.identity_claims.role == "HRManager")
};
