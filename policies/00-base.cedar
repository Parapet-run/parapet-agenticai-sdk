// Base permits.
//
// CEDAR IS DEFAULT-DENY. cedarpy returns Deny unless a `permit` matches, and
// the vendored AGT cedar-lib contains only `forbid` rules. Without this file
// every request denies and the gateway looks broken.
//
// `forbid` always beats `permit`; file order is irrelevant.

permit (principal, action == Action::"model_call", resource);
permit (principal, action == Action::"tool_call", resource);

// Coarse fallback for payloads no parser recognised. Keep this permit narrow
// and alert on its use -- a spike means a provider changed shape.
permit (principal, action == Action::"http_request", resource);
