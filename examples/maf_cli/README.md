# Live example: CLI/batch MAF agent, per-invoker identity (Azure OpenAI)

`run_example.py` drives a real `agent_framework.Agent` (via `GovernedAgent`),
against a real MCP streamable-HTTP server (`conformance/mcp-probe/server.py`,
launched unmodified as a subprocess), governed by the real production Cedar
policies in `policies/` (not a copy made for this demo) via
`src/parapetai_agent/maf.py` -- same governance stack as
`examples/maf_webapp/`, deliberately shaped differently.

## Why a separate example from `maf_webapp/`

`maf_webapp/` is a long-running web server: one process, many concurrent
end users, each request's identity entered ambiently by `IdentityMiddleware`
for exactly the duration of that one HTTP request. That shape doesn't fit a
CLI script or a batch job -- there's no `Request` to wrap, and often no web
framework at all. This example is the other real shape `parapetai_agent` has to
support: **one process, several distinct invokers, each needing their own
identity kept correctly separate across separate `agent.run()` calls** --
`parapetai_agent.identity_store`'s `set_identity()`/`use_identity()`, not
`IdentityMiddleware`.

## The pattern

```python
from parapetai_agent import set_identity, use_identity

set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])  # once, e.g. after "login"
...
with use_identity("alice"):
    await agent.run(...)
```

`set_identity()` persists an identity under a key you choose -- a username,
a thread id, a job id, whatever fits your process's own notion of "who is
this for." `use_identity(key)` looks it up and enters
`parapetai_agent.current_identity()`'s ambient context for exactly the
`agent.run()` call(s) inside that block, then restores whatever was there
before on exit -- see `examples/maf_webapp/README.md`'s "GovernedAgent, and
ambient identity" section for `current_identity()` itself, which this is
built on top of.

## Scenarios

Four identity-store entries, real Cedar decisions via the same production
`policies/30-identity.cedar` role gate `maf_webapp/`'s Entra scenario
exercises, but with synthetic claims instead of a real Entra sign-in --
`verified_synthetic`, not `verified_live` (see `conformance/matrix.yaml`'s own
distinction) -- no tenant needed to run this:

| Invoker | Key | Kind | Roles | Outcome |
|---|---|---|---|---|
| alice | `"alice"` | `CUSTOM` (default) | `["OrderViewer"]` | `lookup_order` **allowed** |
| bob | `"bob"` | `CUSTOM` (default) | `[]` | `lookup_order` **denied** (identity asserted, role missing) |
| carol | `"desk-1"` | `THREAD` | `["OrderViewer"]` | `lookup_order` **allowed** |
| (front desk) | `"desk-1"` | `CUSTOM` | `[]` | `lookup_order` **denied** |

The last two rows are the same key *string* (`"desk-1"`) under two
different `IdentityKeyKind`s, holding two different identities -- the real
proof that `IdentityKeyKind` namespaces a key rather than being cosmetic:
carol's `THREAD:"desk-1"` role grant does not leak into the unrelated
`CUSTOM:"desk-1"` entry that happens to share the same string.

## Local dry run (no Azure needed)

Same pattern as `examples/maf_webapp/`'s own local dry run -- the exact
script, unmodified, against this repo's deterministic fake upstream:

```bash
uv run --with fastapi --with uvicorn python3 conformance/fake-upstream/app.py &

OPENAI_API_KEY=dummy \
OPENAI_BASE_URL=http://127.0.0.1:9001/v1 \
OPENAI_CHAT_COMPLETION_MODEL=fake-model \
uv run --with agent-framework --with "mcp==1.29.0" python3 \
    examples/maf_cli/run_example.py
```

## Azure AI Foundry setup

Copy `.env.example` to `.env` and fill in `AZURE_OPENAI_ENDPOINT`/
`AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_CHAT_COMPLETION_MODEL` -- see
`examples/maf_webapp/README.md`'s "Azure AI Foundry setup" and "Which
model" sections for where each value comes from; the setup is identical,
just without any of that example's web/Entra/Apify-specific variables.

## The wiring is the same minimal shape as every other example, with one exception

```python
agent = GovernedAgent(
    client=OpenAIChatCompletionClient(),
    name=f"{label}-agent",
    instructions="You look up orders. Always call the lookup_order tool.",
    tools=[lookup_order],
    policy_dir=POLICIES,
    local_log_dir=EXAMPLE_DIR / "logs",
)
```

`policy_dir=POLICIES` stays explicit here, unlike `maf_sample_01`-`07`
(which omit it and run on the parapetai-agent bundled default) -- this
example's whole point is real, differentiated Cedar decisions from THIS
repo's own role-gate policy (`policies/30-identity.cedar`), not the
generic base-permits default. `agent_id`/`entities_path`/
`control_plane_url`/`agent_secret` are all omitted -- see
[`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg resolves to. `GovernedAgent`'s resolved
`agent_id` is the SAME value across every invoker in the script -- that's
this SCRIPT's own software-agent identity (one control-plane-provisioned
agent, if configured -- see below), not a separate registered agent per
invoker. alice/bob/carol/front-desk are distinguished from each other
purely by `identity_claims`/`identity_roles` in Cedar's context, exactly
the same way `maf_webapp/`'s many concurrent signed-in end users are all
governed by that ONE app's single `agent_id`.

## Its own agent

Optional -- unset, this script governs entirely off local `policies/`.
Set `PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` in
`.env` (copy `.env.example`) and it instead governs by a real
control-plane-provisioned agent's bundle -- get all three from the
control plane's web UI after provisioning an agent there
(`http://127.0.0.1:8090/agents/new`, printed once right after
provisioning). Deliberately **this example's OWN agent, a separate one
from `maf_webapp/`'s** -- provision two distinct agents (one per example)
rather than sharing a single one, so each example's `.env` names its own
identity and neither example's policy edits on the control plane's
dashboard affect the other. The pulled bundle stays in memory only
(never written to disk, unlike the old `.control-plane-cache/` this
example used to persist to).

## Decision audit logs

Every Cedar decision (allow/deny, determining policy ids, evaluation time)
is logged as a content-free `"decision"` event and persisted to a
size-bounded rotating file under `logs/` via `local_log_dir=` -- see
`examples/maf_webapp/README.md`'s "Decision audit logs" section for the
full shape.
