// Tool invocation control. Only MCP tools/call produces context.tool_name;
// LLM requests carry declarations, not invocations.

@id("tool_destructive_denied")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name &&
  ["delete_file", "drop_table", "execute_shell", "send_payment"].contains(context.tool_name)
};

@id("tool_sql_write_denied")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "query_database" &&
  context has tool_args && context.tool_args has statement &&
  (context.tool_args.statement like "*DROP*" ||
   context.tool_args.statement like "*DELETE*" ||
   context.tool_args.statement like "*TRUNCATE*")
};

// ServiceNow demo: the governed Microsoft Agent Framework agent may read and
// create incidents, but never destroy them or mass-mutate the instance.
// These denies are deterministic and fail-closed -- they hold with no model
// in the loop, which is the whole point vs an evaluator that scores risk.
@id("servicenow_destructive_denied")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name &&
  ["delete_incident", "bulk_close_incidents"].contains(context.tool_name)
};

// Per tool + arguments: closing an incident through a raw update bypasses the
// resolution process -- denied on the argument, not just the tool name.
@id("servicenow_update_close_denied")
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "update_incident" &&
  context has tool_args && context.tool_args has state &&
  ["7", "closed", "resolved"].contains(context.tool_args.state)
};

@id("untrusted_tier_restricted_models")
forbid (principal, action == Action::"model_call", resource)
when {
  context has trust_tier && context.trust_tier == "untrusted" &&
  context has model && context.model like "*opus*"
};
