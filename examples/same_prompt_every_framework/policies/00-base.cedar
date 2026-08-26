// Base permits. Cedar is default-deny: without a permit every call is denied.
permit(principal, action == Action::"model_call", resource);
permit(principal, action == Action::"tool_call", resource);
