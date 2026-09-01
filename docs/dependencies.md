# Dependencies & links

## Packages this repo publishes

| Package | PyPI | What it is |
|---|---|---|
| `parapetai-agent` | [pypi.org/project/parapetai-agent](https://pypi.org/project/parapetai-agent/) | The in-process SDK — `Governor`, `GovernedAgent`, `GovernedRunner`, the Cedar engine, control-plane protocol client, Ed25519 PEP identity. |
| `parapetai-gateway` | not yet published (see below) | The standalone proxy PEP — same Cedar engine, for apps that can't embed it. |
| `parapetai-mcp` | [pypi.org/project/parapetai-mcp](https://pypi.org/project/parapetai-mcp/) | The MCP server + Claude Code skills — login, provisioning, and project scaffolding. |

!!! note "`parapetai-gateway` has no PyPI publish path yet"
    Deliberately deferred until a PyPI trusted publisher is registered
    for it. It's installable from source today
    (`uv run --package parapetai-gateway ...` inside this repo); `uvx
    parapetai-gateway` is the target contract, not yet a CI-verified fact.

## `parapetai-agent` — base install

```toml
dependencies = [
    "cedarpy>=4.0.0,<5.0",
    "structlog>=24.4,<26.0",
    "httpx>=0.28,<1.0",
    "cryptography>=43.0,<47.0",
    "opentelemetry-api>=1.20,<2.0",
]
```

| Dependency | Why |
|---|---|
| [`cedarpy`](https://pypi.org/project/cedarpy/) | The Cedar policy engine itself — every decision runs through this. |
| [`structlog`](https://pypi.org/project/structlog/) | The decision audit log (`"decision"` events) and general structured logging. |
| [`httpx`](https://pypi.org/project/httpx/) | The control-plane protocol client — bundle fetch, heartbeat, review polling. |
| [`cryptography`](https://pypi.org/project/cryptography/) | Ed25519 keypair generation/signing for PEP identity. |
| [`opentelemetry-api`](https://pypi.org/project/opentelemetry-api/) | The audit hook's span attributes — a verified no-op when no tracer is configured, so this is safe to depend on unconditionally without pulling in a full OTel SDK. |

The base install never imports a web framework or an agent framework — a
CLI script or background worker can depend on it without pulling either
in.

## Extras

| Extra | Brings in | For |
|---|---|---|
| `maf` | `agent-framework`, `mcp`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | Microsoft Agent Framework integration ([`GovernedAgent`](reference/governed-agent.md)) and OTel export to the control plane. |
| `adk` | `google-adk`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | Google ADK integration ([`GovernedRunner`](reference/governed-runner.md)) and OTel export. |
| `langgraph` | `langchain`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | LangGraph / LangChain integration ([`ParapetAgentMiddleware`](reference/langgraph.md)) and OTel export. The full `langchain` package, not just `langgraph`/`langchain-core` — `AgentMiddleware` lives in `langchain.agents.middleware`. |
| `web` | `starlette` | `IdentityMiddleware`, JWT bearer extraction for HTTP-fronted agents. |
| `judge` | `litellm` | Provider-agnostic SLM-judge backend (Anthropic, Bedrock, Vertex, Groq, Ollama). Not needed for the default `slm` backend. |
| `dev` | `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`, `opentelemetry-sdk` | Local development / CI only. |
| `docs` | `mkdocs`, `mkdocs-material` | Building this documentation site. Local development / CI only. |

`maf`, `adk`, and `langgraph` are mutually independent — installing one
never pulls in the others' framework SDK. All three source identity
(`scoped_data.py`) and the audit/OTel/registry plumbing
(`governance_runtime.py`) from the same shared modules, so switching
frameworks doesn't mean relearning identity code.

### Not a declared extra, on purpose

The HHEM groundedness backend (`transformers` + `torch`) is deliberately
**not** an extra at all — it would drag the CUDA wheel stack into every
install. Install explicitly:

```bash
pip install transformers torch
```

See [Groundedness (HHEM)](GROUNDEDNESS_HHEM.md).

## Links

| | |
|---|---|
| Repository | [github.com/Parapet-run/parapet-agenticai-sdk](https://github.com/Parapet-run/parapet-agenticai-sdk) |
| Issues | [github.com/Parapet-run/parapet-agenticai-sdk/issues](https://github.com/Parapet-run/parapet-agenticai-sdk/issues) |
| `parapetai-agent` on PyPI | [pypi.org/project/parapetai-agent](https://pypi.org/project/parapetai-agent/) |
| `parapetai-mcp` on PyPI | [pypi.org/project/parapetai-mcp](https://pypi.org/project/parapetai-mcp/) |
| Cedar policy language | [cedarpolicy.com](https://www.cedarpolicy.com/) |
| Model Context Protocol | [modelcontextprotocol.io](https://modelcontextprotocol.io/) |
| Microsoft Agent Framework | [github.com/microsoft/agent-framework](https://github.com/microsoft/agent-framework) |
| Google ADK | [google.github.io/adk-docs](https://google.github.io/adk-docs/) |
| License | MIT — [`LICENSE`](https://github.com/Parapet-run/parapet-agenticai-sdk/blob/main/LICENSE) |

## Python version

`requires-python = ">=3.12"` — tested against 3.12 and 3.13.
