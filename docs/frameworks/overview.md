# Frameworks & support matrix

Parapet enforces the same Cedar engine through **two different
mechanisms**, and which frameworks/languages are supported depends
entirely on which one you're asking about — these are not the same list.

## In-process SDK — Python only

Embeds Cedar directly in the agent's own process. Requires a Python
adapter; today there are three:

| Integration | Class | Framework | Language |
|---|---|---|---|
| Framework-neutral | [`Governor`](../reference/governor.md) | Any (three explicit calls you place yourself) | Python |
| Microsoft Agent Framework | [`GovernedAgent`](../reference/governed-agent.md) | `agent_framework` | Python |
| Google ADK | [`GovernedRunner`](../reference/governed-runner.md) | `google.adk` | Python |

All three call the exact same `policy.engine.PolicyEngine` /
`policy.hooks.GovernanceHook` — the only difference is which framework's
own extension point wires the call in, and how a denial surfaces back to
your code (see each framework's own guide for that — it's genuinely
different per framework, not just a naming difference).

**No non-Python in-process adapter exists.** If your agent isn't Python,
or you can't modify its process, use the gateway instead.

## Gateway PEP — language-agnostic, HTTP interception

`parapetai-gateway` is a standalone proxy: point `OPENAI_BASE_URL` (or
your provider's equivalent env var) at the gateway, and it evaluates the
same Cedar engine as a sidecar — no agent-process code change, no
framework restriction, works for any client library that reads a
provider base-URL from its environment. See
[`gateway/README.md`](https://github.com/Parapet-run/parapet-agenticai-sdk/tree/main/gateway)
in the repo.

Because this path works by intercepting HTTP traffic rather than calling
an SDK function, "supported" here means "the client library actually
reads the env var this needs, and a conformance test proves it routes
through the gateway" — a different, and generally *lower*, bar than the
in-process adapters above, and tracked separately per framework in
[`conformance/matrix.yaml`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/conformance/matrix.yaml).

### Status legend

| Status | Meaning |
|---|---|
| `verified` | A green conformance test at a pinned version. **Only these may be described as "supported" to a customer.** |
| `probable` | Env var documented upstream; no test yet. |
| `unknown` | Needs investigation — assume unsupported. |
| `unsupported` | No config-only lever exists at all. |

Nothing moves to `verified` without a green test — that rule is the
product, not a formality.

### Current matrix

| Framework | Language | Client library | Status | Notes |
|---|---|---|---|---|
| LangGraph / LangChain | Python | `openai-python` (via `langchain-openai`) | ✅ `verified` | `langgraph==1.2.10, langchain-openai==1.4.1`. `create_react_agent` + `ChatOpenAI`, zero gateway-specific code. |
| CrewAI | Python | `openai-python` | ✅ `verified` | `crewai==1.15.10`, **OpenAI path only** — Anthropic/Gemini/Azure/Bedrock paths are not yet verified individually. |
| AutoGen | Python | `openai-python` | ✅ `verified` | `autogen-agentchat==0.7.5, autogen-ext[openai]==0.7.5`. |
| OpenAI Agents SDK | Python | `openai-agents` | 🟡 `probable` | Official docs state the default provider reads `OPENAI_BASE_URL`; no green test yet. |
| Google ADK | Python | `google-genai` | ⚪ `unknown` | `GOOGLE_GEMINI_BASE_URL`/`GOOGLE_VERTEX_BASE_URL` work but are undocumented upstream; Vertex mode has a known auth mismatch. |
| Microsoft Agent Framework | Python | `azure-openai` | ⚪ `unknown` | Azure path shape differs (`/openai/deployments/{d}/chat/completions`) — not the same as this repo's in-process `GovernedAgent`, see note below. |
| LlamaIndex | Python | `openai-python` | ⚪ `unknown` | — |
| Haystack | Python | `openai-python` | ⚪ `unknown` | `OpenAIGenerator` takes `api_base_url`; env fallback unconfirmed. |
| Azure AI Foundry | Python | `azure-ai-projects` | ⚪ `unknown` | — |
| Mastra | **TypeScript** | `vercel-ai-sdk` | ⚪ `unknown` | The gateway path is not Python-only — this is the one non-Python row in the matrix today. |
| Dify | n/a (platform config) | — | ⚪ `unknown` | Configured through Dify's model-provider admin UI, not env vars — still config-not-code, but a different integration surface entirely. |

!!! danger "Don't describe `unknown`/`probable` as supported"
    This is a hard rule, not a style preference (see `CONTRIBUTING.md` and
    this repo's own working agreements): every framework/language claim
    needs a fixture or a conformance test, not an assertion in a doc.

## Don't conflate the MAF row with `GovernedAgent`

The **gateway's** `maf` row above (`unknown`, Azure OpenAI base-URL
interception) is a completely different thing from this SDK's **in-process**
[`GovernedAgent`](../reference/governed-agent.md) (verified, exercised
directly by this repo's own test suite). If someone asks "is MAF
supported," the honest answer depends on which enforcement point they
mean:

- In-process (`GovernedAgent`, embed Cedar in the agent's own process): **yes**, this is one of only two first-class adapters this SDK ships.
- Gateway (proxy interception of MAF's own Azure OpenAI traffic): **unknown**, not yet conformance-tested.

## Choosing between them

| | In-process SDK | Gateway |
|---|---|---|
| Can you modify the agent's process? | Yes | No, or you'd rather not |
| Language | Python only | Any (per the matrix above) |
| Denial surfacing | Native to the framework (exception, synthetic response, etc. — see each guide) | HTTP-level (blocked request/response) |
| Setup | `pip install parapetai-agent[maf\|adk]`, swap a class | Set a base-URL env var, run the gateway |

## Next

- [Governor guide](governor.md) — the framework-neutral path
- [Microsoft Agent Framework guide](maf.md)
- [Google ADK guide](adk.md)
