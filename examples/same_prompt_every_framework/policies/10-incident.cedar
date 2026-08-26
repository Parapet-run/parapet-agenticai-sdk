// The ONE rule under test, and every framework is judged by it.
// Reading an incident informs; deleting one destroys the record of what
// happened. An agent may do the first and never the second.
@id("delete_incident_denied")
forbid (principal, action == Action::"tool_call", resource)
when { context has tool_name && context.tool_name == "delete_incident" };
