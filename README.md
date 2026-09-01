# parapet-agenticai-sdk

[![CI](https://github.com/Parapet-run/parapet-agenticai-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/Parapet-run/parapet-agenticai-sdk/actions/workflows/ci.yml)
[![Docs](https://github.com/Parapet-run/parapet-agenticai-sdk/actions/workflows/docs.yml/badge.svg)](https://parapet-run.github.io/parapet-agenticai-sdk/)

📖 **[Full documentation](https://parapet-run.github.io/parapet-agenticai-sdk/)** — installation, quickstart, framework guides (MAF/ADK), the `parapetai-mcp` CLI, and the complete API reference.

**In-process runtime governance for AI agents.** Wrap the agent you already
have, and every model call and tool call becomes a [Cedar](https://www.cedarpolicy.com/)
policy decision — **default-deny, fail-closed, content-free audit** — enforced
inside your process, before anything happens.

```bash
pip install parapetai-agent
```

> Python import name: `parapetai_agent`. Repo: `Parapet-run/parapet-agenticai-sdk`.

Parapet is the enforcement point that lives *inside* the agent. A control tower
can observe your fleet and, at worst, kill an agent; the network gateway can
inspect traffic at the wire. Neither can decide — deterministically, in the
process, before the fact — whether *this* caller may take *this* action with
*these* arguments, and stop just that one call while the agent keeps working.
That decision is what this SDK makes.

---

## What it does

Three governance surfaces, one Cedar decision each, all in-process:

| Stage | Question | Mechanism |
|---|---|---|
| **Input** (`pre`) | Should the model even see this prompt? | PII / secrets / injection / profanity scanners + a Cedar `model_call` decision (topic scope, trust tier) — **before** the model is called. |
| **Tool call** | May the model run *this* tool with *these* args, as *this* caller? | Cedar `tool_call` authorization by name, arguments, and identity role. A denied call never executes. |
| **Output** (`post`) | Is the answer grounded and on-policy? | Groundedness (HHEM / lexical) + an SLM judge score the response; a Cedar `post`-stage decision applies their verdicts **before** the user sees a word. |

Every decision produces a **content-free, signed audit record** — verdict,
determining policy, stage, identity, latency, policy generation. Your prompts
and the model's responses never leave the process.

## Quickstart

### Fastest path: scaffold it with Claude Code + `parapetai-mcp`

If you're using [Claude Code](https://claude.com/claude-code), skip writing
any of this by hand. `parapetai-mcp` is an MCP server that logs in to a
control plane, provisions an agent, and either generates a runnable demo
project from nothing, retrofits Parapet into a project you already have, or
audits one for ungoverned model/tool calls — Claude does the file edits.

```bash
pipx install parapetai-mcp
parapetai-mcp init         # installs the parapet-* skills into .claude/skills/
claude mcp add parapet -e PARAPETAI_CONTROL_PLANE_URL=https://app.parapet.run -- parapetai-mcp serve
```

Then, in Claude Code:

- **"Build me a Parapet demo"** → the `parapet-quickdemo` skill generates a
  small, runnable, identity-based governance project from scratch (Google
  ADK, Microsoft Agent Framework, or LangGraph/LangChain — your choice),
  against a real control plane you can click into.
- **"Add Parapet to my agent"** (in an existing `agent_framework` or
  `google.adk` project) → the `parapet-maf` / `parapet-adk` skill
  provisions an agent and instruments your existing code.
- **"Audit my codebase for governance risks"** → the `parapet-audit` skill
  runs a local, read-only static scan (no control-plane call) for
  ungoverned model/tool calls, scored high/medium/low; `parapet-audit-fix`
  then wraps the flagged sites in `GovernedAgent`/`GovernedRunner` as a
  separate, explicit step.

No `pipx`? `brew install pipx` (macOS) or
`python3 -m pip install --user pipx && pipx ensurepath`. Full reference:
[parapetai-mcp docs](https://parapet-run.github.io/parapet-agenticai-sdk/cli/parapetai-mcp/).

### Any framework — `Governor`

Three calls at whatever hook points your framework already has. No adapter, no
framework dependency — works with LangGraph, CrewAI, the OpenAI Agents SDK, or
a plain `while` loop:

```python
from parapetai_agent import Governor, GovernanceDenied

# Policy authored in the control plane, pulled and kept fresh in the background.
gov = Governor.from_control_plane(
    "https://control.parapet.example",
    agent_secret="...",           # issued once at provisioning
    policy_dir="./policies",      # seed + where the last-known-good bundle lives
    persist_policy_dir="./policies",
)

gov.check_input(prompt, roles=["OrderViewer"])   # before the model
gov.authorize_tool("delete_incident", {...})     # before a tool runs -> may raise
gov.check_output(answer, sources=[doc])          # after the model
```

Every decision is evaluated **locally, in-process** — the control plane is
never on the decision path, so it can be down without blocking a call. When it
*is* unreachable at startup, the agent falls back to the last bundle on disk
and keeps enforcing it; with nothing on disk there is no policy to enforce, and
it fails closed rather than running ungoverned.

For local development or an air-gapped install, use
`Governor.from_policy_dir("./policies")` instead — same three calls, policy
from files you manage.

Denials raise `GovernanceDenied`; pass `raise_on_deny=False` to get the
`Decision` back and branch on it yourself.

### Three outcomes, not two — allow, deny, **review**

A policy can hold a call for a person instead of refusing it outright. Annotate
a `forbid` with `@action("review")` in the control plane, and the SDK raises
`GovernanceReviewRequired` with a ticket you can wait on:

```python
from parapetai_agent import Governor, GovernanceReviewRequired

gov = Governor.from_control_plane(policy_dir="./policies")

try:
    gov.authorize_tool("transition_issue", {"issue": "INC-42", "state": "closed"})
except GovernanceReviewRequired as held:
    print(held.review_id)                       # queued for a human, agent NOT blocked

    if gov.wait_for_approval(held, timeout=300):  # opt in to blocking
        transition_issue("INC-42")                # approved — valid for THIS call, once
```

The parts worth knowing:

- **A held call is a deny until someone approves it.** `Decision.allowed` stays
  `False`, and `GovernanceReviewRequired` **subclasses `GovernanceDenied`** — so
  code written before approvals existed keeps blocking a held call. Upgrading
  the SDK can never start executing one.
- **It does not block by default.** You get a ticket and continue;
  `wait_for_approval()` is opt-in. It returns `False` for *every* non-approval
  (denied, expired, timed out, control plane unreachable), so there is one thing
  to check and the safe answer is the default.
- **A grant is single-use and bound to that exact call.** Approving "close
  INC-42" cannot be replayed onto INC-43, and cannot be spent twice.
- **An unreachable control plane cannot soften a decision.** Cedar still decides
  locally; if the queue is unreachable, `review_id` is `None` and the call stays
  denied. Approvals are something a connected PEP gains, never something local
  enforcement depends on.
- **Prompts are never sent to the queue.** A tool call's arguments are shown to
  the approver (they are what the policy matched on); a `check_input` /
  `check_output` call sends only a digest.

See **[docs/adr/0009](docs/adr/0009-approval-loop.md)** for the design.

### A specific framework — `GovernedAgent` / `GovernedRunner`

Pick your framework and install its extra; the rest of the interface stays the
same — same `agent_id=` / `policy_dir=` / `control_plane_url=` kwargs, same
`GovernanceDenied`, same identity API, whichever you choose. `maf` and `adk`
are independent: installing one never pulls in the other's SDK.

**Microsoft Agent Framework** (`pip install parapetai-agent[maf]`) —
`GovernedAgent` is a drop-in replacement for `agent_framework.Agent`:

```python
from parapetai_agent import GovernedAgent as Agent, GovernanceDenied

agent = Agent(
    name="support",
    instructions="Help the customer.",
    tools=[lookup_order],
    agent_id="pa-e3931c464751",
    control_plane_url="https://control.parapet.example",
    agent_secret="...",           # issued once at provisioning
)

try:
    result = await agent.run("Where is order 1234?")
except GovernanceDenied as denied:
    print(denied.reason)          # e.g. "servicenow_destructive_denied"
```

Already have your own middleware chain? `build_middleware()` returns the same
governance as a plain middleware:

```python
from parapetai_agent import build_middleware

mw = build_middleware(
    agent_id="pa-e3931c464751",
    control_plane_url="https://control.parapet.example",
    agent_secret="...",
)
agent = SomeFrameworkAgent(..., middleware=[mw])
```

**Google ADK** (`pip install parapetai-agent[adk]`):

```python
from parapetai_agent.adk import GovernedRunner as Runner

runner = Runner(
    app_name="support",
    agent=root_agent,
    session_service=session_service,
    agent_id="pa-e3931c464751",
    control_plane_url="https://control.parapet.example",
    agent_secret="...",
)

async for event in runner.run_async(user_id="alice", session_id=sid, new_message=message):
    if event.error_code == "governance_denied":
        print(event.error_message)
```

`GovernedAgent` and `GovernedRunner` are drop-in replacements for each
framework's own `Agent`/`Runner`. The class differs because each framework puts
its governable seam in a different place (MAF: `Agent(middleware=[...])`; ADK:
`Runner(plugins=[...])`), not because the integration differs. Building your own
chain instead? `build_middleware()` (MAF) and `build_plugin()` (ADK) return the
same governance to wire in yourself. Reaching for
`google.adk.runners.InMemoryRunner`? `parapetai_agent.adk.InMemoryGovernedRunner`
mirrors it exactly — same in-memory session/artifact/memory defaults, plus
governance.

## Can't change the app? Use the gateway

The SDK and the [gateway](gateway/) are the **same enforcement role in two
form factors** — both evaluate the same Cedar engine locally, in-process.
Embed the SDK when you can modify the agent; run the gateway when you can't,
or when the agent isn't Python at all.

```bash
uvx parapetai-gateway                                    # or run the container
export OPENAI_BASE_URL=http://localhost:8080/a/<agent-id>/v1   # in the app
```

That is the whole integration — no code change, and it works for a Node, Go,
or Java agent that could never `pip install` anything. They live in one repo
deliberately: the gateway imports this package's engine, parsers, and identity,
so splitting them is how the engine forks.

## Identity

Decisions are made about a **caller**, not just an agent. Bind one:

```python
from parapetai_agent import set_identity, use_identity

set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])
with use_identity("alice"):
    await agent.run(...)
```

In a web app, install `parapetai-agent[web]` and add `IdentityMiddleware`, which
lifts the caller identity off the incoming request (JWT/OIDC) automatically.

## Control plane — integration is an HTTP API

The SDK is useful stand-alone (point `policy_dir=` at local Cedar files), but in
production it speaks a small signed HTTP protocol to a **control plane** that
distributes policy and receives the audit stream. The SDK is the client; the
control plane is a separate service.

- **Policy in:** the SDK pulls a signed policy **bundle** (`GET /api/v1/bundle`),
  caches it to disk, and hot-loads it into the engine. Requests are signed with
  the agent's Ed25519 key; bundle freshness is an ETag (`304 Not Modified`).
- **Presence:** a periodic heartbeat (`POST /api/v1/fleet/heartbeat`) reports the
  enforcing policy generation and can carry a key-rotation signal back.
- **Audit / telemetry out:** content-free decision records reach the control
  plane as OTLP spans and logs (`POST /v1/traces`, `/v1/logs`) — see below.

Full endpoint reference, auth, and the signing contract: **[docs/CONTROL_PLANE_API.md](docs/CONTROL_PLANE_API.md)**.

## OTel to the control plane

Governance decisions are emitted as OpenTelemetry spans and shipped to the
control plane's OTLP receiver — the same standard OTLP/HTTP wire format any
collector speaks, so you can fan out to your own backend too.

```python
from parapetai_agent import configure_otel

configure_otel(
    service_name="support-agent",
    otlp_endpoint="https://control.parapet.example",   # -> /v1/traces, /v1/logs
    agent_secret="...",                                # sent as Bearer, identifies the agent
)
```

`build_middleware()` calls this **for you** once `control_plane_url` / `agent_secret`
resolve — `otlp_endpoint` defaults to `PARAPETAI_OTLP_ENDPOINT`, else the control
plane host. The spans are content-free by construction. Details and the span
schema: **[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)**.

## Extras

| Extra | Brings in | For |
|---|---|---|
| `maf` | `agent-framework`, `mcp`, OpenTelemetry SDK + OTLP exporter | Microsoft Agent Framework integration and OTel export |
| `adk` | `google-adk`, OpenTelemetry SDK + OTLP exporter | Google ADK integration and OTel export |
| `web` | `starlette` | `IdentityMiddleware`, JWT bearer extraction |
| `judge` | `litellm` | The provider-agnostic SLM-judge backend — Anthropic, Bedrock, Vertex, Groq, Ollama. Not needed for the default `slm` backend, which speaks the OpenAI wire. |
| _(base)_ | `cedarpy`, `httpx`, `cryptography`, `opentelemetry-api` | Cedar engine, control-plane protocol client, Ed25519 PEP identity |

The base install never imports a web framework or an agent framework — a CLI
script or background worker can depend on it without pulling either in.

## Invariants

These are security properties, not defaults you can tune away:

- **Fail closed.** An unparsed payload, an evaluation error, or a missing policy
  denies. No exception path becomes an implicit allow.
- **Cedar is default-deny.** No matching `permit` is a Deny; `forbid` always
  beats `permit`.
- **A bad bundle never empties the policy set.** Reload keeps the previous
  policies on failure.
- **Prompt content is never logged** unless you explicitly opt in. The decision
  audit record is content-free by construction, not by configuration.
- **A REVIEW is a deny, not a soft allow.** `Decision.allowed` is `False` for
  `effect == "review"`, so a held call does not execute and any caller that
  only checks `allowed` blocks it exactly as it blocks a denial. A review needs
  unanimity: if any determining policy is a plain `forbid`, the deny stays hard.
  See [ADR 0008](docs/adr/0008-review-decision-outcome.md).

## Project layout

```
src/parapetai_agent/
  govern.py           # Governor — the framework-neutral entry point (any framework)
  _exceptions.py      # GovernanceDenied / GovernanceReviewRequired — catchable
                      #   without importing any framework
  maf.py              # GovernedAgent, build_middleware, configure_otel — the MAF integration
  policy/             # Cedar engine, request/decision shapes, stage split
  content_checks.py   # PII / secrets / injection / profanity scanners (input guardrails)
  groundedness.py     # output groundedness (lexical default, HHEM optional)
  _hhem.py            # Vectara HHEM-2.1 backend (local or in-VPC service)
  response_judge.py   # SLM judge (rubric-scored output evals)
  identity.py, identity_middleware.py, token_identity.py, identity_store.py
  pep_identity.py     # Ed25519 PEP keypair (load/create/rotate)
  signing.py          # the exact bytes a PEP and control plane sign/verify
  control_plane.py    # PEP -> control-plane HTTP client (bundle pull, heartbeat, key register)
  otel/               # OpenInference span conventions
gateway/              # the PROXY PEP -- same Cedar engine, for apps that can't embed
mcp-server/           # parapetai-mcp: MCP server + SKILL.md for Claude Code
tests/                # pytest suite
conformance/          # per-framework proof the block happens in the real runtime
policies/             # Cedar sources used as engine fixtures
examples/             # runnable integrations: maf_sample_01..07, maf_cli,
                      #   adk_sample_01, adk_webapp, ungoverned_vs_governed.
                      #   The ones taking control_plane_url + agent_secret
                      #   exercise provisioned identity end to end.
docs/                 # ADRs, observability, the MAF integration pattern
docs/                 # API + observability + architecture references, plus ADRs
```

## Docs

📖 **[parapet-run.github.io/parapet-agenticai-sdk](https://parapet-run.github.io/parapet-agenticai-sdk/)**
— installation, quickstart, framework guides, the `parapetai-mcp` CLI/skills
reference, and the full `Governor` / `GovernedAgent` / `GovernedRunner` /
`Decision` API reference, all in one hosted site. The pages below are the
same source files, browsable directly in the repo:

- [Architecture](docs/ARCHITECTURE.md) — stages, fail-closed, the trust boundary
- [Control-plane API](docs/CONTROL_PLANE_API.md) — the HTTP protocol the SDK speaks
- [Observability / OTel](docs/OBSERVABILITY.md) — decisions as content-free spans
- [Groundedness / HHEM](docs/GROUNDEDNESS_HHEM.md) — the output-faithfulness backends
- [ADR 0006](docs/adr/0006-cedar-policy-stage-and-action-annotations.md) — `@stage` / `@action` policy annotations
- [ADR 0008](docs/adr/0008-review-decision-outcome.md) — REVIEW as a third decision outcome
- [Examples](examples/) — a runnable authorization demo (base install, no model)
- [Contributing](CONTRIBUTING.md)

## Links

- Docs: https://parapet-run.github.io/parapet-agenticai-sdk/
- Source: https://github.com/Parapet-run/parapet-agenticai-sdk
- Issues: https://github.com/Parapet-run/parapet-agenticai-sdk/issues

MIT licensed.
