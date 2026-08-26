# parapet-agenticai-sdk

[![CI](https://github.com/Parapet-run/parapet-agenticai-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/Parapet-run/parapet-agenticai-sdk/actions/workflows/ci.yml)

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

### Microsoft Agent Framework — `GovernedAgent`

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
  _exceptions.py      # GovernanceDenied — catchable without importing any framework
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
tests/                # pytest suite
conformance/          # per-framework proof the block happens in the real runtime
policies/             # Cedar sources used as engine fixtures
docs/                 # API + observability + architecture references, plus ADRs
```

## Docs

- [Architecture](docs/ARCHITECTURE.md) — stages, fail-closed, the trust boundary
- [Control-plane API](docs/CONTROL_PLANE_API.md) — the HTTP protocol the SDK speaks
- [Observability / OTel](docs/OBSERVABILITY.md) — decisions as content-free spans
- [Groundedness / HHEM](docs/GROUNDEDNESS_HHEM.md) — the output-faithfulness backends
- [ADR 0006](docs/adr/0006-cedar-policy-stage-and-action-annotations.md) — `@stage` / `@action` policy annotations
- [ADR 0008](docs/adr/0008-review-decision-outcome.md) — REVIEW as a third decision outcome
- [Examples](examples/) — a runnable authorization demo (base install, no model)
- [Contributing](CONTRIBUTING.md)

## Links

- Source: https://github.com/Parapet-run/parapet-agenticai-sdk
- Issues: https://github.com/Parapet-run/parapet-agenticai-sdk/issues

MIT licensed.
