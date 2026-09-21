# Installation

## Requirements

- Python **3.12** or **3.13** (`requires-python = ">=3.12"` in `pyproject.toml`).
- No web framework or agent framework is required for the base install —
  `pip install parapetai-agent` alone never imports one.

## Install the SDK

```bash
pip install parapetai-agent
```

That's the base install: the Cedar engine (`cedarpy`), the control-plane
protocol client, and Ed25519 PEP identity (`cryptography`). It never
imports a web framework or an agent framework, so a CLI script or
background worker can depend on it without pulling either in.

### Pick an extra for your framework

Framework integrations are opt-in extras — installing one never pulls in
the other's framework SDK:

```bash
pip install "parapetai-agent[maf]"   # Microsoft Agent Framework
pip install "parapetai-agent[adk]"   # Google ADK
```

| Extra | Brings in | For |
|---|---|---|
| `maf` | `agent-framework`, `mcp`, OTel SDK + OTLP exporter | Microsoft Agent Framework integration and OTel export |
| `adk` | `google-adk`, OTel SDK + OTLP exporter | Google ADK integration and OTel export |
| `otel` | OTel SDK + OTLP exporter | Just decision telemetry export (to a control plane or collector), with no agent framework. The `maf`/`adk`/`langgraph` extras already include it |
| `web` | `starlette` | `IdentityMiddleware`, JWT bearer extraction |
| `judge` | `litellm` | Provider-agnostic SLM-judge backend (Anthropic, Bedrock, Vertex, Groq, Ollama) — not needed for the default `slm` backend |
| `dev` | `pytest`, `ruff`, `mypy`, ... | Local development / CI only |

**Telemetry export needs an extra.** The base install enforces policy and audits
every decision locally (structured logs), and carries only `opentelemetry-api`,
which is a no-op. Sending decisions as OpenTelemetry spans and logs to a control
plane or collector needs the OpenTelemetry SDK and OTLP exporter, which the
framework extras already include and the `otel` extra provides on its own:

```bash
pip install "parapetai-agent[otel]"
```

Without it, everything still works: `Governor.from_control_plane()` keeps pulling
policy, sending heartbeats and enforcing, and logs
`otel_sdk_missing_telemetry_export_disabled` to say decisions are not being
exported. Calling `configure_otel()` directly raises an `ImportError` that names
this extra.

`maf` and `adk` are mutually independent: `pip install parapetai-agent[adk]`
alone must work without ever importing `agent_framework`, and vice versa.
Both source identity and audit/OTel plumbing from the same shared modules,
so switching frameworks later doesn't mean relearning identity code.

The HHEM groundedness backend (`transformers` + `torch`) is deliberately
**not** a declared extra — it would drag the CUDA wheel stack into every
install. Enable it explicitly:

```bash
pip install transformers torch
```

See [Groundedness (HHEM)](../GROUNDEDNESS_HHEM.md) for the two
output-faithfulness backends and when you'd want the heavier one.

## Working in this repo (contributors)

This repo is a [uv](https://docs.astral.sh/uv/) workspace: the root
package **is** the published `parapetai-agent` (not restructured into a
member), plus two workspace members — `gateway/` (`parapetai-gateway`) and
`mcp-server/` (`parapetai-mcp`).

```bash
git clone https://github.com/Parapet-run/parapet-agenticai-sdk
cd parapet-agenticai-sdk
make install        # uv sync --all-extras
```

!!! warning "A bare `uv sync` installs the root only"
    `gateway/` and `mcp-server/` tests run under `uv run --package <name>`.
    Running them from the root venv fails with
    `ModuleNotFoundError: parapetai_gateway` — see the commands below.

### Dev loop

```bash
make install         # uv sync --all-extras
make test             # test-sdk + test-gateway
make test-sdk          # uv run --extra maf --extra adk --extra judge --extra dev pytest tests -q
make test-gateway        # uv run --package parapetai-gateway --extra dev pytest gateway/tests -q
make lint                 # ruff check (src, tests, gateway/src, gateway/tests, and examples/ deliberately)
make typecheck              # mypy --strict on src and gateway/src
make check                    # lint + typecheck + test -- what CI runs
make conformance                # the live cross-framework subset of tests/test_maf.py
make docs                         # build this documentation site (mkdocs build --strict)
make docs-serve                    # live-reload preview of this documentation site
```

Single test:

```bash
uv run --extra maf --extra adk --extra judge --extra dev pytest tests/test_adk.py -v
uv run --extra maf --extra adk --extra judge --extra dev pytest tests -k "GovernedAgent and not live" -v
uv run --package parapetai-gateway --extra dev pytest gateway/tests/test_streaming.py -v
```

## Next

- [Quickstart](quickstart.md) — your first governed call, local-only, no
  control plane.
- [Frameworks & support matrix](../frameworks/overview.md) — which
  frameworks and languages are supported, and by which enforcement point.
