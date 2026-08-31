# Quickstart

This walks through the smallest useful governed call, fully local — no
control plane, no network call, just Cedar evaluating a policy file on
disk. It works identically whether you end up using [`Governor`](../frameworks/governor.md)
directly, [`GovernedAgent`](../frameworks/maf.md) (MAF), or
[`GovernedRunner`](../frameworks/adk.md) (ADK) — this page uses the
framework-neutral `Governor`, since it has no framework SDK to install
first.

## 1. Install

```bash
pip install parapetai-agent
```

## 2. Write a policy

Cedar is **default-deny**: with no matching `permit`, everything denies.
Create `policies/00-base.cedar`:

```cedar title="policies/00-base.cedar"
permit (principal, action == Action::"model_call", resource);
permit (principal, action == Action::"tool_call", resource);
```

## 3. Make governed calls

```python title="quickstart.py"
from parapetai_agent import Governor

gov = Governor.from_policy_dir("./policies")

# 1. Before the model ever sees the prompt
decision = gov.check_input("What's the weather in Paris?")
print(decision.allowed, decision.effect)   # True allow

# 2. Before a tool actually runs
decision = gov.authorize_tool("get_weather", {"city": "Paris"})
print(decision.allowed, decision.effect)

# 3. Before the model's answer reaches the caller
decision = gov.check_output("It's 18°C and cloudy.")
print(decision.allowed, decision.effect)
```

Every one of those three calls returns a
[`Decision`](../reference/decision.md) — same dataclass, same fields,
regardless of which of the three integration surfaces produced it. By
default (`raise_on_deny=True`, the default on every call) a deny raises
[`GovernanceDenied`](../reference/exceptions.md) with the `Decision`
attached as `.decision`, rather than returning a falsy value you might
forget to check.

## 4. Scope it to a caller

A policy is only interesting once it can see *who* is calling. Add an
org-scoped rule to a second file, `policies/10-scope.cedar`:

```cedar title="policies/10-scope.cedar"
forbid (principal, action == Action::"tool_call", resource)
when {
  context has tool_name && context.tool_name == "get_weather" &&
  !(context has identity_claims &&
    context.identity_claims has org &&
    context.identity_claims.org == "Ops")
};
```

Then pass a `Caller` scoped to that claim:

```python
from parapetai_agent.identity import Caller

gov = Governor.from_policy_dir("./policies", caller=Caller(agent_id="weather-bot"))
```

For per-*end-user* identity inside a shared agent process (not just a
static caller for the whole process), see `governed_identity()` in the
[`GovernedAgent`](../frameworks/maf.md) / [`GovernedRunner`](../frameworks/adk.md)
guides — the same context-manager pattern applies whichever framework you
use.

## 5. Swap in a real framework

Once this works, the framework-specific wrappers are strictly less code,
not more — they replace the three explicit calls above with one context
manager / one middleware registration around your existing agent loop.
See:

- [Microsoft Agent Framework guide](../frameworks/maf.md) — `GovernedAgent`
- [Google ADK guide](../frameworks/adk.md) — `GovernedRunner`
- [Frameworks overview & support matrix](../frameworks/overview.md) — what's
  verified today, and what the gateway path covers for frameworks with no
  in-process adapter yet

## 6. See it running end to end, without writing any of this yourself

If you have [Claude Code](https://claude.com/claude-code) with the
`parapetai-mcp` server connected, the fastest way to see governance work
end to end — a real Cedar policy, a real allow, a real deny, on a real
control plane you can click into — is the `parapet-quickdemo` skill. It
generates a small runnable project for you. See [parapetai-mcp](../cli/parapetai-mcp.md).
