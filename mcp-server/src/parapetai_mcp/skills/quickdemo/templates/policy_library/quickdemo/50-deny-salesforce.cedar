// OPTIONAL local test policy -- not pushed to the control plane, only
// loaded when PARAPETAI_MODE=local (see example_governed.py / README.md).
//
// Denies salesforce_lookup entirely, for every caller regardless of org.
// Stacks with 40-org.cedar's existing per-org gate (forbid always wins,
// unconditionally, over any permit -- see the SDK's PolicyEngine header).
// Effect on the demo's four scripted scenarios: Tony's own salesforce_lookup
// call flips from ALLOWED to DENIED; Sally's was already denied by
// 40-org.cedar, so it stays denied. Useful for testing "we're retiring this
// tool" without touching the control plane bundle.

@id("deny_salesforce_lookup_entirely")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "salesforce_lookup"
};
