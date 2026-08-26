// The whole point of the demo: a destructive tool the agent must never call,
// no matter what the model decides. Deterministic, content-free (matches on the
// tool NAME only), and it beats the base permit above.
@id("destructive_tool_denied")
forbid (principal, action == Action::"tool_call", resource)
when { ["delete_records", "drop_table", "execute_shell"].contains(context.tool_name) };
