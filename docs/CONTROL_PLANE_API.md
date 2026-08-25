# Control-plane API

The SDK enforces locally, but in production it talks to a **control plane** — a
separate service that distributes signed policy bundles and receives the
content-free audit stream. This document is the contract between the two: the
HTTP endpoints, how they are authenticated, and the Ed25519 signing scheme.

The client lives in `parapetai_agent/control_plane.py`; identity and signing in
`parapetai_agent/pep_identity.py` and `parapetai_agent/signing.py`.

## Roles

- **PEP (Policy Enforcement Point)** — your agent process, running this SDK. It
  *pulls* policy and *pushes* decisions. It never receives commands.
- **Control plane** — issues agent credentials, stores per-agent bundles,
  verifies signatures, and ingests the audit/telemetry stream.

Trust flows one way: the PEP authenticates itself to the control plane. The
control plane is authenticated by TLS.

## Credentials

Two secrets, provisioned once and never re-shown:

| Credential | Created by | Held by | Used for |
|---|---|---|---|
| `agent_id` + `agent_secret` | Control plane, at provisioning (`POST /api/v1/agents`) | The agent (config / env) | Bearer auth on every call; only the secret's *hash* is stored server-side |
| Ed25519 keypair | The agent, on first run (`pep_identity.load_or_create_keypair`) | Private key never leaves the agent | Signing bundle-pull and heartbeat requests |

The private key is written to `~/.parapetai/pep_ed25519.key` (`0600`), overridable
via `PARAPETAI_PEP_KEY_PATH`. Where no filesystem is writable (e.g. Lambda), an
ephemeral in-memory key is used instead — still a stable identity for the
process lifetime.

## Endpoints

### Management API — prefix `/api/v1`

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/agents` | Operator / admin | Provision an agent. Returns `agent_id` + one-time `secret`. Usually run by a CLI, not the SDK. |
| `POST /api/v1/keys` | Bearer `agent_secret` | Register this PEP's Ed25519 **public** key. Idempotent; rotation demotes the previous key so in-flight requests still verify. |
| `GET /api/v1/bundle` | Bearer + **signed** | Pull the agent's current signed policy bundle. Send `If-None-Match: <etag>`; a `304 Not Modified` means keep the cached bundle. |
| `POST /api/v1/fleet/heartbeat` | Bearer + **signed** | Report liveness + the enforcing policy generation/digest. Response may carry `rotate_key: true`. |
| `GET /api/v1/fleet` | Dashboard | Fleet listing (control-plane UI). |
| `POST /api/v1/audit` | Bearer `agent_secret` | Ingest content-free decision records (alternative to / alongside the OTLP path). |
| `GET /api/v1/audit` | Dashboard | Query recent audit records. |

### OTLP receiver — standard paths

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /v1/traces` | Bearer `agent_secret` | Ingest OpenTelemetry **spans** (the decision stream). Standard OTLP/HTTP protobuf. |
| `POST /v1/logs` | Bearer `agent_secret` | Ingest OpenTelemetry **logs**. |

The bearer secret in the `Authorization` header is what identifies *which*
agent's spans/logs these are. See [OBSERVABILITY.md](OBSERVABILITY.md).

## Request signing

Once a PEP has registered a public key, every subsequent bundle-pull and
heartbeat **must** carry a valid signature (gradual enforcement: an agent with
no registered key is served unsigned, for backwards compatibility, until it
registers one).

Two headers are added:

```
X-Parapetai-Signed-At: <RFC3339 timestamp string>
X-Parapetai-Signature: <base64 Ed25519 signature>
```

The signed bytes are exactly (`parapetai_agent/signing.py`):

```
signing_payload(method, path, signed_at, body) =
    b"{METHOD}\n{path}\n{signed_at}\n" + body
```

- `method` upper-cased; `path` is the request path only (no query string).
- `signed_at` is the literal header **string**, signed byte-for-byte — both
  sides use the same string, never a re-parsed float, so formatting can't drift.
- `body` is the raw request body (empty for a bodyless `GET`).

The control plane verifies against the agent's current *or* previous registered
key, within a bounded clock-drift window that limits replay.

## Lifecycle (typical)

```
provision (operator)         POST /api/v1/agents        -> agent_id + secret
first run  (agent)           POST /api/v1/keys          register public key
steady state (agent loop)
  every N seconds            GET  /api/v1/bundle         (signed)  -> bundle or 304
                             POST /api/v1/fleet/heartbeat (signed) -> ok / rotate_key
  per decision               POST /v1/traces, /v1/logs   content-free spans/logs
```

`run_bundle_poller()` in `control_plane.py` drives the steady-state loop: it
fetches, writes the bundle to `policy_dir` for restart persistence, hot-applies
it to the live `PolicyEngine`, and heartbeats — all with the same signing key.

## Running without a control plane

Everything above is optional. Point the SDK at local Cedar files and it enforces
with no network at all:

```python
from parapetai_agent import build_middleware
mw = build_middleware(policy_dir="./policies")   # no control_plane_url / secret
```

Bundle pull, heartbeat, and remote audit simply don't run. Decisions can still
be exported to any OTLP endpoint you configure yourself.
