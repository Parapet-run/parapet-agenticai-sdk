---
name: parapetai-agent
description: Wire parapetai-agent Cedar governance into a Python Microsoft Agent Framework (agent_framework) agent. Use when adding policy enforcement, MCP governance, or a PEP to an agent; when the user mentions parapetai-agent, GovernedAgent, governed_identity, IdentityMiddleware, PARAPETAI_ env vars, Parapet middleware, or asks to govern agent/tool traffic. Covers construction, identity selection, .env.example authoring, and verification.
---

# Integrating parapetai-agent

`parapetai-agent` governs a Microsoft Agent Framework agent's model calls and tool
calls with Cedar policy. It works by splicing two `agent_framework` middleware
objects (`ParapetChatMiddleware`, `ParapetFunctionMiddleware`) into the agent at
construction time.

**The integration is small.** Swap `Agent` for `GovernedAgent`, pick an identity
method, write `.env.example`. Do not build more than that.

## Preflight — stop conditions

Run these checks before writing anything. If any fails, report it and stop.

1. **`agent_framework` must be a dependency.** Check `pyproject.toml`,
   `requirements.txt`, or imports. This SDK governs Microsoft Agent Framework
   agents only. There is no LangChain, LlamaIndex, CrewAI, or raw-OpenAI-SDK
   integration. If the project uses one of those, say so plainly and stop —
   do not improvise a wrapper.
2. **Python project.** No JS/TS equivalent exists.
3. **Locate every construction site.** Find where `agent_framework.Agent(...)`
   (or a subclass) is instantiated. These are the only lines you will change.
   If there are several, enumerate them for the user before editing.

## Step 1 — Install

Use whatever the project already uses (`uv`, `poetry`, `pip`, `pdm`). Add
`parapetai-agent` to the project's dependency file rather than only installing it
into the environment.

## Step 2 — Swap the constructor

```python
from parapetai_agent import GovernedAgent

agent = GovernedAgent(
    client=...,            # unchanged — any agent_framework chat client
    name="...",            # unchanged
    instructions="...",    # unchanged
    tools=[...],           # unchanged, if present
)
```

Those three kwargs are the entire required surface. Every other `GovernedAgent`
parameter is optional and defaults to working, disk-free behaviour with real
Cedar enforcement from construction. `GovernedAgent` accepts and passes through
`agent_framework`'s own kwargs (`tools`, `context_providers`, `middleware`)
unchanged.

**Preserve existing kwargs exactly.** If the original call passed
`context_providers=` or `middleware=`, keep them. User middleware runs *after*
governance, never instead of it.

**Do not touch `run()` call sites.** `GovernedAgent` does not override `run()`.
Streaming and non-streaming both work unchanged:

```python
result = await agent.run(query)                      # non-streaming
async for chunk in agent.run(query, stream=True):    # streaming
    ...
```

Cedar evaluation happens as a side effect of `run()`. There is no separate
check/authorize/evaluate call to add. If you find yourself writing one, you have
misunderstood the design.

## Step 3 — Choose the identity method

This is the only real judgment call. Choose from observable properties of the
codebase, not from what sounds thorough.

| What you see in the project | Use | Import |
|---|---|---|
| FastAPI/Starlette app, many concurrent end users | `IdentityMiddleware` wired once on the app | `from parapetai_agent import IdentityMiddleware` |
| One process, several distinct invokers (users/threads/jobs) | `set_identity()` / `use_identity()` | `from parapetai_agent import set_identity, use_identity, IdentityKeyKind` |
| One-shot script with an inbound JWT | `governed_identity(token=jwt)` | `from parapetai_agent import governed_identity` |
| One-shot script already holding an azure-identity credential | `governed_identity(credential=cred)` | same |
| Already-decoded claims from your own session store | `governed_identity(claims=..., roles=[...])` | same |
| Caller has per-call identity in hand and wants no ambient state | `agent.run(q, function_invocation_kwargs={"identity_claims": ..., "identity_roles": ...})` | none |
| Demo, smoke test, or no identity concept exists yet | nothing — omission is the signal | none |

Shapes:

```python
# web server — wired ONCE, not per route
def _extract_identity(request) -> tuple[Mapping, Sequence] | None:
    session = sessions.get(request.cookies.get("session_id"))
    if session is None or not session["identity_claims"]:
        return None
    return session["identity_claims"], session["identity_roles"]

app.add_middleware(IdentityMiddleware, extractor=_extract_identity)

# multi-invoker CLI
set_identity("alice", claims={"oid": "..."}, roles=["OrderViewer"])
with use_identity("alice"):
    result = await agent.run(query)

# one-shot
with governed_identity(token=jwt):
    result = await agent.run(query)
```

`governed_identity()` takes **exactly one** source kwarg (`claims`+`roles`,
`token`, or `credential`). Zero or more than one raises `ValueError`.

### Two things to tell the user, not silently assume

- **No identity is not a bypass.** With no identity set, Cedar evaluates against
  empty `identity_claims`/`identity_roles`. Under the bundled default policy
  (base permits) that runs fine. The moment a policy checks identity, it becomes
  a **deny**. If you wire no identity, say this out loud in your summary.
- **`token=` and `credential=` can change the Cedar principal.** A decoded JWT is
  read for end-user claims *and* for a delegation signal (RFC 8693 `act`, or a
  bare `azp`/`appid`). A delegation signal **overrides** `agent_id`. An ordinary
  `az login` token often carries `appid` with no real delegation chain. Flag this
  and suggest the user decode a real token before relying on the behaviour.

### One hard scaling rule

If the app runs as more than one replica (check for a Dockerfile with a replica
count, a Procfile with `web: gunicorn -w N`, k8s manifests, `uvicorn --workers`)
and you used `set_identity`/`use_identity`, you **must** call
`configure_identity_store()` with a shared backend (Redis, a database). The
default in-memory store is invisible across replicas and silently reverts to
anonymous rather than erroring — a silent authorization failure. Do not leave
this as a TODO; either configure it or tell the user explicitly.

## Step 4 — Author `.env.example`

Create or extend `.env.example` at the project root. **Placeholders only — never
real secret values, and never write a `.env` with live credentials.**

```dotenv
# --- parapetai-agent governance ---
# Cedar principal for this agent. Optional: unset -> Agent::"anonymous".
PARAPETAI_AGENT_ID=

# Control plane. Set BOTH of the next two to opt into a real control plane:
# policy bundle pull + automatic OTel export of parapetai.model_call /
# parapetai.tool_call spans. Leave both unset for bundled-policy-only local
# enforcement (still real Cedar).
PARAPETAI_CONTROL_PLANE_URL=
PARAPETAI_AGENT_SECRET=

# Optional. PEP Ed25519 identity key. Unset -> ~/.parapetai/pep_ed25519.key
# Irrelevant when constructing with persist_pep_key=False.
PARAPETAI_PEP_KEY_PATH=
```

Then append the model provider block for the client actually in use — only the
one that matches:

| Client in the code | Add |
|---|---|
| `OpenAIChatCompletionClient` (OpenAI) | `OPENAI_API_KEY=` |
| `OpenAIChatCompletionClient` (Azure) | `AZURE_OPENAI_ENDPOINT=` and `AZURE_OPENAI_API_KEY=` |
| `FoundryChatClient` | `FOUNDRY_PROJECT_ENDPOINT=` and `FOUNDRY_MODEL=` |
| `AnthropicClient` | `ANTHROPIC_API_KEY=` |
| `GeminiChatClient` | `GEMINI_API_KEY=` |

Finally, confirm `.env` is in `.gitignore`. If it is not, add it. If a `.env`
already exists and is tracked by git, stop and warn the user rather than
editing it.

## Step 5 — Optional, only on explicit signal

Add these **only** when something in the project demands it. Adding them
speculatively is the most common way this integration goes wrong.

| Add | Only when |
|---|---|
| `policy_dir=` / `entities_path=` | The repo already contains `.cedar` files the user wants used. Otherwise the bundled default policy applies automatically. |
| `persist_pep_key=False` | Read-only filesystem: serverless, distroless, or a container with a read-only root. |
| `persist_policy_dir=` | A control plane is configured **and** a writable volume is mounted **and** the user wants the pulled bundle cached across restarts. |
| `local_log_dir=` | The user asked for a local decision-audit file. Console output happens regardless. |
| `otel_log_mode="streaming"` | The user prefers per-record latency over request volume. Default `"buffered"` batches to 512 records or 120s. |
| explicit `configure_otel(...)` | Custom `service_name`, Azure Monitor export, or batch tuning. Must be called **before** constructing `GovernedAgent` — first call wins, process-wide. |
| `flush_otel()` in shutdown | Long-running server. `atexit` does not fire on SIGTERM; put it in the `finally:` of a lifespan handler. |

## Never do these

- **Never author Cedar policy.** The SDK bundles and wires it
  (`parapetai_agent.policy.default_policies`). Writing `.cedar` files, generating
  entities, or setting `policy_dir` to a directory you created is wrong.
- **Never add an explicit policy-check call.** Enforcement is middleware.
- **Never wrap `run()` in a `try/except` that swallows a denial.** A deny is the
  system working. Converting it to a fallback path defeats the product.
- **Never call `configure_otel()` after `GovernedAgent` construction** — the
  first registration wins and yours will be silently ignored.
- **Never write real secrets** into `.env.example`, source, or committed config.
- **Never claim identity is wired** when you only added `PARAPETAI_AGENT_ID`. That is
  the agent principal, not end-user identity.

## Verify before reporting done

1. The project imports and builds/type-checks with whatever the repo already uses.
2. `GovernedAgent` is constructed with `client`, `name`, `instructions` present.
3. No `run()` call site was modified.
4. Pre-existing `middleware=` / `context_providers=` kwargs survived the edit.
5. `.env.example` contains the three `PARAPETAI_` keys and exactly one provider block.
6. `.env` is gitignored.
7. Run `pep doctor --json` if the CLI is available and resolve reported issues.

## Report to the user

State plainly: which construction sites changed, which identity method you chose
and why, whether identity is wired at all (and the deny consequence if not),
whether a control plane is configured or bundled-policy-only, and anything from
the "tell the user" items above. Point them at `git diff`.

## Quick reference — full construction surface

```python
GovernedAgent(
    client=...,                 # required
    name="...",                 # required
    instructions="...",         # required
    tools=[...],                # optional, passthrough
    context_providers=[...],    # optional, passthrough
    middleware=[...],           # optional, passthrough — runs AFTER governance
    policy_dir=None,            # None -> bundled default policy
    entities_path=None,         # None -> bundled entities.json
    agent_id=None,              # None -> PARAPETAI_AGENT_ID, else Agent::"anonymous"
    tenant="default",
    control_plane_url=None,     # None -> PARAPETAI_CONTROL_PLANE_URL
    agent_secret=None,          # None -> PARAPETAI_AGENT_SECRET
    pep_key_path=None,          # None -> PARAPETAI_PEP_KEY_PATH, else ~/.parapetai/pep_ed25519.key
    persist_policy_dir=None,    # None -> pulled bundle stays in memory
    persist_pep_key=True,       # False -> ephemeral Ed25519 identity, nothing written
    local_log_dir=None,         # None -> no local rotating audit file
    otel_log_mode="buffered",   # or "streaming"
)
```

Tool sources are governed identically and need no configuration: plain Python
functions (`native`), `MCPStreamableHTTPTool`, `MCPStdioTool`, `MCPWebsocketTool`.
Swapping the chat client does not change governance.