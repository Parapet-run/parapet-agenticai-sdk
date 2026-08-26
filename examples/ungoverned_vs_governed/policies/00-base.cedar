// Base permits. Cedar is default-deny; without a matching permit every request
// is denied. These permits make the agent usable; the forbid in 10-destructive
// then carves out the actions that must never happen (forbid beats permit).
permit (principal, action == Action::"model_call", resource);
permit (principal, action == Action::"tool_call", resource);
