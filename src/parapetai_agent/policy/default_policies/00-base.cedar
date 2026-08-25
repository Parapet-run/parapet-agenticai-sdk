// Bundled default policy -- what GovernedAgent/build_middleware() (in
// the parapetai-agent package) governs with when the caller omits policy_dir
// entirely (see parapetai_agent.maf's _bundled_default_policy_dir()).
// Lives here, in parapetai-agent, not parapetai-agent -- co-located with
// PolicyEngine (the class that loads it), and available to ANY
// consumer of parapetai-agent's policy engine, not just the in-process
// embedding path this was first built for (gateway/'s own standalone
// PEP could reuse this exact same fallback someday without depending on
// parapetai-agent at all). A real, read-only file shipped inside the
// parapetai-agent package itself -- not written at runtime, so this works
// identically in a read-only container filesystem (k8s, serverless)
// with no writable volume, unlike a control-plane-pulled bundle
// persisted to disk.
//
// CEDAR IS DEFAULT-DENY. cedarpy returns Deny unless a `permit` matches.
// Without this file every request denies and a newly-wired GovernedAgent
// looks broken with zero explanation. `forbid` always beats `permit`;
// file order is irrelevant.
//
// This is a STARTING POINT, not a real deployment's actual policy --
// once a real control-plane-provisioned agent_id/control_plane_url/
// agent_secret are configured, the agent's real, control-plane-owned
// bundle takes over (see build_middleware()'s own docstring for exactly
// when).

permit (principal, action == Action::"model_call", resource);
permit (principal, action == Action::"tool_call", resource);

// Coarse fallback for payloads no parser recognised. Keep this permit
// narrow and worth alerting on in a real deployment -- a spike means a
// provider changed shape.
permit (principal, action == Action::"http_request", resource);
