# Parapet

**Cedar-governed AI agents.** Parapet puts an authorization decision in
front of every model call and every tool call an agent makes — scoped to
*who* is calling, evaluated by a real [Cedar](https://www.cedarpolicy.com/)
policy engine, denying by default.

```python
from parapetai_agent import GovernedAgent
from parapetai_agent.scoped_data import governed_identity

async with GovernedAgent(
    client=client,
    name="workplace-agent",
    instructions="...",
    tools=[salesforce_lookup, hr_lookup],
    policy_dir="./policies",
) as agent:
    with governed_identity(claims={"org": "Sales", "name": "Tony"}):
        result = await agent.run("Look up the ACME opportunity")
```

Tony gets Salesforce. He's denied HR. Nothing in the agent's own code
changed to make that true — a Cedar policy scoped to `org` did.

## Two ways to enforce, one engine

Every decision — in-process or over the wire — runs through the exact same
[`policy.engine.PolicyEngine`](reference/decision.md), so the guarantees
below hold identically no matter which one you pick.

<div class="grid cards" markdown>

-   :material-code-braces: **In-process SDK** (`parapetai-agent`)

    ---

    Embed Cedar directly in your agent's own process. Three integration
    surfaces: the framework-neutral [`Governor`](frameworks/governor.md),
    [`GovernedAgent`](frameworks/maf.md) for Microsoft Agent Framework, and
    [`GovernedRunner`](frameworks/adk.md) for Google ADK. Python only —
    pick this when you can modify the agent process.

    [:octicons-arrow-right-24: Get started](getting-started/installation.md)

-   :material-server-network: **Gateway PEP** (`parapetai-gateway`)

    ---

    A standalone proxy: point `OPENAI_BASE_URL` (or your provider's
    equivalent) at the gateway and it evaluates the same Cedar engine as a
    sidecar, with no agent-process code change and no framework
    restriction. Pick this for a non-Python agent, or one you don't
    control. See [`gateway/README.md`](https://github.com/Parapet-run/parapet-agenticai-sdk/tree/main/gateway) in the repo.

</div>

## The path of one request

```
prompt
  │
  ▼
[ identity ]        who is calling? every decision is scoped to a caller
  │
  ▼
[ input  · pre  ]   PII/secrets/injection scanners + Cedar model_call decision
  │                 DENY → the model never sees the prompt
  ▼
[ model call    ]
  │
  ▼
[ tool   · call ]   Cedar tool_call authorization, per tool, by name+args+role
  │                 DENY → the tool never runs; agent continues
  ▼
[ output · post ]   groundedness (HHEM/lexical) + SLM judge → Cedar post decision
  │                 DENY → the answer is withheld
  ▼
response (only if every gate allowed)
```

Full narrative, including the fail-closed invariants that hold at every
one of those gates: [Architecture](ARCHITECTURE.md).

## Non-negotiable invariants

These hold across every integration surface, not just some of them:

1. **Fail closed.** An unparsed payload, an evaluation error, or a missing
   policy denies. No exception path becomes an implicit allow.
2. **Cedar is default-deny.** No matching `permit` is a Deny; `forbid`
   always beats `permit`, unconditionally, regardless of file order.
3. **A bad or unreachable policy bundle never empties the policy set.**
   Reload keeps the previous generation on failure.
4. **Prompt/response content is never logged unless explicitly opted in.**
   The decision audit record is content-free by construction, not by
   configuration.
5. **`REVIEW` is a deny, not a soft allow.** `Decision.allowed` stays
   `False` for a review; unanimity across every determining policy is
   required for a hard deny to become reviewable at all.
6. **An unreachable control plane cannot soften an enforcement decision.**
   It can cost you an approval opportunity or a policy refresh; it can
   never turn a deny into an allow.

## Where to go next

| I want to... | Go to |
|---|---|
| Install the SDK and run a first governed call | [Installation](getting-started/installation.md) → [Quickstart](getting-started/quickstart.md) |
| See which frameworks and languages are supported today | [Frameworks & support matrix](frameworks/overview.md) |
| Wire up Microsoft Agent Framework | [MAF guide](frameworks/maf.md) |
| Wire up Google ADK | [ADK guide](frameworks/adk.md) |
| Use the MCP server / Claude Code skills to scaffold a project | [parapetai-mcp](cli/parapetai-mcp.md) |
| Look up every constructor argument for `GovernedAgent`, `GovernedRunner`, `Decision`, etc. | [API Reference](reference/governor.md) |
| See every environment variable this repo reads | [Environment variables](reference/env-vars.md) |
| Understand the two-plane split (this SDK vs. the control plane) | [Architecture](ARCHITECTURE.md) |

## Source and license

MIT licensed, public:
[`github.com/Parapet-run/parapet-agenticai-sdk`](https://github.com/Parapet-run/parapet-agenticai-sdk).
Policy *authoring*, tenancy, billing, and fleet/audit aggregation live in a
separate, private control-plane product — this repo and its published
packages (`parapetai-agent`, `parapetai-gateway`, `parapetai-mcp`) are the
entire enforcement side, and are never required to talk to that product:
`Governor.from_policy_dir()` runs fully local, with zero network calls.
