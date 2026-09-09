# Corroboration (`corroboration`)

`parapetai_agent.corroboration` — since **0.8.0**, Tier 1 only. Turns on
real OpenTelemetry auto-instrumentation for common HTTP/gRPC client
libraries, so a real network call a tool makes during its own execution
emits a span correctly correlated to that tool's own `parapetai.tool_call`
span — the **observed** half of the vendor/CRUD story, layered under the
[declared](vendor-calls.md) `@declare_vendor_call` signal.

```bash
pip install "parapetai-agent[corroboration]"
```

```python
from parapetai_agent import configure_otel
from parapetai_agent.corroboration import enable_http_corroboration

configure_otel(otlp_endpoint="...")  # call this FIRST — see Ordering below
enable_http_corroboration()
```

## What it does and doesn't do

**Does**: makes the signal exist and land in the right place in the trace
tree. Every adapter already wraps a tool call in
`tracer.start_as_current_span("parapetai.tool_call")`; OTel's context
propagation attaches any span started during that window to it as a
child. Once instrumented, a real outbound `httpx`/`requests`/`urllib3`/
`aiohttp`/`grpc` call made *inside* a tool's own execution shows up as
exactly that — a child span, correlated via `parent_span_id`, flowing to
the control plane over the same OTLP `/v1/traces` pipe the tool_call span
and its decision record already use.

**Does not**: compare the resulting span's host/method against a tool's
declared `crud_action`, or revoke a classification, or re-queue anything
for triage. That comparison is deliberately left as separate,
not-yet-built work — a control-plane analysis problem over
already-correlated span data once this signal exists, not new SDK
machinery.

## Coverage

Five official `opentelemetry-python-contrib` instrumentors, HTTP/gRPC
transports only (the database instrumentors it also ships —
`-psycopg2`/`-pymongo`/`-redis`/`-sqlalchemy` — observe a tool's *own*
datastore, not a downstream vendor API call, a different question this
module doesn't address):

| Library | Instrumentor |
|---|---|
| `httpx` | `opentelemetry-instrumentation-httpx` |
| `requests` | `opentelemetry-instrumentation-requests` |
| `urllib3` | `opentelemetry-instrumentation-urllib3` |
| `aiohttp` (client) | `opentelemetry-instrumentation-aiohttp-client` |
| `grpc` | `opentelemetry-instrumentation-grpc` |

Each candidate self-detects whether its target library is installed
(`BaseInstrumentor`'s own dependency check) and no-ops rather than raising
if not — safe to call `enable_http_corroboration()` unconditionally in any
process, whether it uses one of these transports, all five, or none.

**Reaches through arbitrary vendor-SDK wrapping depth.** These
instrumentors patch the transport library's own methods, not whatever
application code calls them — so `simple-salesforce` (on `requests`),
`boto3` (on `botocore` → `urllib3`), or a generated gRPC client are all
covered with zero awareness of that SDK's existence. The real limit is
context propagation, not library detection: the span attaches to whichever
OTel context is current when the call happens, which works automatically
for a tool running synchronously (or in the same asyncio task) as its own
`tool_call` span, but is **not** guaranteed if a vendor SDK hands the I/O
off to its own thread pool or subprocess without copying that context
across.

**No signal ≠ no mismatch.** A tool call using a transport outside this
set (or a C-extension driver like `psycopg2`'s libpq binding, which does
its own socket I/O beneath the Python socket module entirely) simply
produces no child span — indistinguishable, at this tier, from a call that
matched its declaration correctly. Absence of a signal is not proof of a
match.

## API

```python
def enable_http_corroboration() -> dict[str, bool]: ...
def disable_http_corroboration() -> None: ...
def http_corroboration_enabled() -> bool: ...
```

- **`enable_http_corroboration()`** — idempotent and safe to call more
  than once (e.g. from more than one `build_middleware()`/`build_plugin()`
  call in the same process). Returns `{library_name: whether it actually
  activated}` for every candidate — `False` means either the instrumentor
  package itself isn't installed, or its target library isn't installed in
  the calling application; both are ordinary, not errors. Per-library
  `ERROR`-level lines OTel's own instrumentor logs for a missing target are
  suppressed for the duration of this call (this module logs its own
  `http_corroboration_enabled` summary instead) — five `ERROR` lines on
  every startup for transports a typical agent doesn't use would read as
  something broken.
- **`disable_http_corroboration()`** — reverses it; real, exported
  functionality, not a test fixture. Needed by your own test suite if it
  reconfigures OTel between cases — see Ordering below for why. Safe to
  call even if nothing was ever enabled.
- **`http_corroboration_enabled()`** — `True` once
  `enable_http_corroboration()` has been called in this process, regardless
  of how many (if any) candidates actually activated.

## Ordering: call this *after* `configure_otel()`, never before

Confirmed live, not assumed: each contrib instrumentor resolves and
**captures** a real `Tracer` reference inside its own `instrument()` call
— unlike this SDK's own adapters, whose tracer is a lazy proxy that
re-resolves against whatever provider is globally current at every
`start_as_current_span()` call. A contrib instrumentor does **not**
re-resolve later. If `enable_http_corroboration()` runs before
`configure_otel()` (or any other call that registers the real
`TracerProvider`), every span it ever emits goes to whatever no-op/default
tracer was active at that earlier moment — permanently, until the process
re-instruments.

```python
# Correct order
configure_otel(otlp_endpoint="...")
enable_http_corroboration()

# Wrong — spans silently go nowhere
enable_http_corroboration()
configure_otel(otlp_endpoint="...")
```

This also applies to `GovernedAgent`/`GovernedRunner`'s automatic OTel
auto-wiring (which fires once `control_plane_url`/`agent_secret` both
resolve) — call `enable_http_corroboration()` after constructing the
agent/runner, not before.

## Not yet built

- **Tier 2** (socket/TLS monkeypatch — catches any pure-Python library
  with no dedicated OTel package) — deferred; not needed to cover the
  common case, where a real OTel instrumentor already exists for most
  integrations.
- **Tier 3** (eBPF or a transparent proxy — the only way past a
  C-extension driver like `psycopg2`) — deliberately out of scope for this
  in-process SDK: it conflicts with this project's own stated
  differentiator, ["no proxy in the data path"](../ARCHITECTURE.md). If
  ever built, it extends `parapetai-gateway` (a separate published
  package), not this module.
- **The comparison itself** — declared `crud_action` vs. observed
  `host`/`method`, and any enforcement/revocation consequence of a
  mismatch — is a control-plane feature, not an SDK one; see "What it does
  and doesn't do" above.

## See also

- [Vendor/CRUD metadata](vendor-calls.md) — the declared half this signal
  corroborates.
- [Automatic vendor/resource/permission detection](vendor-scope-permission.md) —
  builds on these same spans to classify a call's vendor/product/resource/
  permission without any `@declare_vendor_call` at all.
- [Observability](../OBSERVABILITY.md) — `configure_otel()` and the OTLP
  export pipeline this rides.
