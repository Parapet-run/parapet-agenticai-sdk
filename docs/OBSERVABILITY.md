# Observability — OTel to the control plane

Every governance decision is emitted as an OpenTelemetry span. Those spans are
shipped to the control plane's OTLP receiver (`/v1/traces`, `/v1/logs`) over the
standard OTLP/HTTP protobuf wire format — so the same stream can fan out to any
collector you already run.

The defining property: **the telemetry is content-free.** A span carries the
*decision*, never the data the decision was about. Prompts and model responses
do not appear in it. That is what lets the stream cross the trust boundary — out
of your process, into a governance backend — without exposing customer data.

## Configuring export

```python
from parapetai_agent import configure_otel

configure_otel(
    service_name="support-agent",
    otlp_endpoint="https://control.parapet.example",   # base URL
    agent_secret="...",                                # Bearer -> identifies the agent
)
```

`configure_otel` appends the OTLP spec's standard per-signal paths to the base
URL — `{otlp_endpoint}/v1/traces` and `{otlp_endpoint}/v1/logs` — and sends the
agent secret as `Authorization: Bearer <secret>`, which is how the receiver
attributes the spans to your agent.

### It's automatic under `build_middleware()`

When you pass `control_plane_url` / `agent_secret` (or set
`PARAPETAI_CONTROL_PLANE_URL` / `PARAPETAI_AGENT_SECRET`), `build_middleware()`
calls `configure_otel()` for you:

- `otlp_endpoint` defaults to `PARAPETAI_OTLP_ENDPOINT` if set, otherwise the
  resolved `control_plane_url` (same host, standard OTLP paths).
- the resolved `agent_secret` is used as the bearer token.

So the common case needs no explicit `configure_otel` call — governance
decisions start flowing to the control plane as soon as it's wired up.

### Export elsewhere too

`otlp_endpoint` is just an OTLP/HTTP endpoint. Point it at your own collector,
or configure additional exporters on the tracer provider, to send the same
content-free decision stream to your existing observability backend.

## What a decision span contains

Attributes follow OpenInference-style conventions (`parapetai_agent/otel/`). A
decision span carries, in the content-free spirit:

- **verdict** — allow / deny
- **stage** — `pre` (input), `tool_call`, or `post` (output)
- **action** — `model_call` or `tool_call`, and the tool name when applicable
- **determining policy** — the Cedar policy id that decided it (empty on a
  default-deny — no rule matched — which is itself diagnostic)
- **identity** — the caller principal / roles the decision was scoped to
- **policy generation / bundle digest** — which policy version enforced
- **latency** — how long the decision took

What it never contains: the prompt text, the tool arguments' values, or the
model's response. Only the shape of the decision crosses the wire.

## Dependencies

Real OTLP export needs the exporter, which rides in the `maf` extra:

```bash
pip install "parapetai-agent[maf]"
```

The base package depends only on `opentelemetry-api` — `get_current_span()` is a
verified no-op when no SDK/tracer is configured, so importing the core without
any OTel backend is safe and adds nothing at runtime.

## Troubleshooting

- **No spans at the control plane.** Confirm the `maf` extra is installed (the
  base package has no exporter), and that `control_plane_url`/`agent_secret`
  resolved — without them `build_middleware()` skips `configure_otel()`.
- **401 at `/v1/traces`.** The `agent_secret` bearer is wrong or unprovisioned;
  it's the same secret used for the management API.
- **Decisions enforced but not observed.** Enforcement (Cedar) and export (OTel)
  are independent — a decision is *made* even if the exporter isn't configured.
  Wire `configure_otel` (or the control-plane args) to see them.
