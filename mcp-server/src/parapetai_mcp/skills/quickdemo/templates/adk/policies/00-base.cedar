// CEDAR IS DEFAULT-DENY -- without a base permit, EVERY tool_call denies
// with "no permit matched or forbid applied", even for the ALLOWED
// scenarios (Tony -> salesforce_lookup, Sally -> hr_lookup). This mirrors
// two things that are otherwise easy to forget when testing purely
// locally (PARAPETAI_MODE=local):
//   1. The starter bundle a freshly `parapet_provision_agent`'d agent gets
//      on the control plane (cloud mode's baseline before 40-org.cedar's
//      forbid rules narrow it -- see SKILL.md step 4's own explanation).
//   2. parapetai_agent's own bundled default policy
//      (src/parapetai_agent/policy/default_policies/00-base.cedar), used
//      when GovernedAgent/GovernedRunner is called with no policy_dir at
//      all. Kept byte-for-byte identical to that file on purpose.
//
// 40-org.cedar's forbid rules narrow this exactly the same way in both
// modes -- `forbid` always beats `permit`, unconditionally, regardless of
// file order (see parapetai_agent/policy/engine.py's own header).

permit (principal, action == Action::"model_call", resource);
permit (principal, action == Action::"tool_call", resource);

// Coarse fallback for payloads no parser recognised.
permit (principal, action == Action::"http_request", resource);
