// OPTIONAL local test policy -- not pushed to the control plane, only
// loaded when PARAPETAI_MODE=local (see example_governed.py / README.md).
//
// Denies hr_lookup entirely, for every caller regardless of org. Stacks
// with 40-org.cedar's existing per-org gate (forbid always wins,
// unconditionally, over any permit). Effect on the demo's four scripted
// scenarios: Sally's own hr_lookup call flips from ALLOWED to DENIED;
// Tony's was already denied by 40-org.cedar, so it stays denied. The
// mirror image of 50-deny-salesforce.cedar -- pick one, the other, both,
// or neither.

@id("deny_hr_lookup_entirely")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "hr_lookup"
};
