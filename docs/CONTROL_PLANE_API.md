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
| `agent_id` + `agent_secret` | Issued once by the control plane at provisioning, out of band | The agent (config / env) | Bearer auth on every call; only the secret's *hash* is stored server-side |
| Ed25519 keypair | The agent, on first run (`pep_identity.load_or_create_keypair`) | Private key never leaves the agent | Signing bundle-pull and heartbeat requests |

The private key is written to `~/.parapetai/pep_ed25519.key` (`0600`), overridable
via `PARAPETAI_PEP_KEY_PATH`. Where no filesystem is writable (e.g. Lambda), an
ephemeral in-memory key is used instead — still a stable identity for the
process lifetime.

## Endpoints

### Agent API — prefix `/api/v1`

This is the **complete** protocol a PEP speaks. Every endpoint here is
agent-authenticated: a bearer `agent_secret`, and for the two that matter most,
an Ed25519 signature as well.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/keys` | Bearer `agent_secret` | Register this PEP's Ed25519 **public** key. Idempotent; rotation demotes the previous key so in-flight requests still verify. |
| `GET /api/v1/bundle` | Bearer + **signed** | Pull the agent's current signed policy bundle. Send `If-None-Match: <etag>`; a `304 Not Modified` means keep the cached bundle. |
| `POST /api/v1/fleet/heartbeat` | Bearer + **signed** | Report liveness + the enforcing policy generation/digest. Response may carry `rotate_key: true`. |
| `POST /api/v1/audit` | Bearer + **signed** | Ingest content-free decision records. An alternative to the OTLP path below; this SDK uses OTLP. |

The control plane exposes other routes — provisioning, the operator console,
tenant and fleet administration. They are **not** part of this protocol, are not
callable with an agent secret, and are deliberately not documented here: an
adopter never needs them, and this SDK never calls them. `agent_id` and
`agent_secret` are issued to you once at provisioning, out of band.

#### Optional `details` on the heartbeat

`POST /api/v1/fleet/heartbeat` may carry an optional `details` object: kind-specific,
content-free status. It is **omitted entirely** by every PEP that has nothing extra to
say, so an existing PEP sends exactly what it always did, and a control plane that
predates the field ignores it. Today only `parapetai-gateway` sends one
(`"kind": "gateway"`):

```json
{
  "kind": "gateway", "schema": 1, "site": "site-a",
  "config": {"mtls": true, "client_auth": "required", "identity_required": true},
  "tls": {"generation": 3, "last_reload_at": 1789900000.0, "last_error": null,
          "server_cert": {"subject": "gw.example.com", "issuer": "…", "serial": "…",
                          "sha256": "…", "not_before": 0.0, "not_after": 0.0},
          "client_ca": [{"subject": "…", "sha256": "…", "not_after": 0.0}]},
  "connected": [{"agent_id": "fib-sales", "method": "mtls", "subject": "fib-sales",
                 "requests": 41, "denied": 2, "held_for_review": 0,
                 "first_seen": 0.0, "last_seen": 0.0}],
  "identity_refusals": {"invalid_token": 2},
  "recent_events": [{"ts": 0.0, "type": "tls_reloaded", "generation": 3}]
}
```

It is **content-free**: agent ids, identity methods, certificate metadata and counts,
never a request, a prompt, a tool argument, a token, or private key material. A control
plane should treat it as untrusted network input: store only a known `kind`, cap its
size, and never merge it across heartbeats (the latest block replaces the previous one).

#### `vendor_scoped_resources` on the bundle response

Since 0.7.0, the `GET /api/v1/bundle` response may carry a top-level
`vendor_scoped_resources: bool` field — lets a control-plane operator turn
on [opt-in vendor-scoped Cedar resource construction](reference/vendor-calls.md)
for a tenant without a code change on the PEP side. Resolved **once, at
bootstrap** by `Governor.from_control_plane()` / `bootstrap_engine()` —
unlike `.cedar` policy/entities content, it does not hot-reload mid-process
on a later poll; a tenant-level change takes effect on this PEP's next
full bootstrap (process restart).

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
provision (operator)         out of band                -> agent_id + secret
first run  (agent)           POST /api/v1/keys          register public key
steady state (agent loop)
  every N seconds            GET  /api/v1/bundle         (signed)  -> bundle or 304
                             POST /api/v1/fleet/heartbeat (signed) -> ok / rotate_key
  per decision               POST /v1/traces, /v1/logs   content-free spans/logs
```

`run_bundle_poller()` in `control_plane.py` drives the steady-state loop: it
fetches, writes the bundle to `policy_dir` for restart persistence, hot-applies
it to the live `PolicyEngine`, and heartbeats — all with the same signing key.

## Multi-agent gateway API (proposed)

!!! warning "Proposed contract — not implemented yet"
    Nothing in this section exists in the control plane today. It is the
    agreed contract for a **multi-agent gateway**: one `parapetai-gateway`
    process enforcing for many agents of one tenant. The single-agent
    protocol above is unchanged and stays supported; an existing PEP or
    gateway keeps working with its current `agent_secret`.

### Why a separate credential

An `agent_secret` resolves to exactly one `agent_id`. A gateway fronting many
agents needs its own credential, scoped so that a compromised gateway can act
only inside one tenant.

| Credential | Scope | Used for |
|---|---|---|
| `gateway_id` + `gateway_secret` | **One tenant.** May serve only agents whose `tenant_id` matches. | Bearer auth on every `/api/v1/gateway/*` call and OTLP ingest |
| Gateway Ed25519 keypair | Same | Signing requests (same scheme as [Request signing](#request-signing)) |

Unlike agents, signing is **mandatory from the first call**: there is no
unsigned grace period for a gateway. Until a key is registered, only
`POST /api/v1/gateway/keys` is accepted.

### Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/gateway/keys` | Bearer `gateway_secret` | Register the gateway's Ed25519 public key (same rotation semantics as `/keys`). |
| `GET /api/v1/gateway/config` | Bearer + **signed** | Tenant identity config: IdP settings and identity→agent bindings. Supports `If-None-Match`. |
| `POST /api/v1/gateway/bundles` | Bearer + **signed** | Pull bundles for a batch of agents in one call. |
| `POST /api/v1/gateway/heartbeat` | Bearer + **signed** | Liveness plus the enforcing generation/digest **per agent**. |
| `POST /api/v1/gateway/reviews`, `POST /api/v1/gateway/reviews/{id}/collect` | Bearer + **signed** | Submit and collect an approval, with `agent_id` in the body. |
| `POST /v1/traces`, `POST /v1/logs` | Bearer `gateway_secret` | OTLP ingest; each record names its agent (see below). |

#### `GET /api/v1/gateway/config`

```json
{
  "gateway_id": "gw-…",
  "tenant_id": "tenant-…",
  "digest": "<sha256 of everything below>",
  "identity_providers": [
    {
      "issuer": "https://login.microsoftonline.com/<tid>/v2.0",
      "jwks_uri": "https://login.microsoftonline.com/<tid>/discovery/v2.0/keys",
      "audiences": ["api://parapet-gateway"],
      "algorithms": ["RS256"],
      "agent_claims": ["azp", "appid"],
      "user_claims": {"oid": "oid", "tid": "tid"},
      "roles_claim": "roles"
    }
  ],
  "bindings": [
    {"kind": "jwt",  "issuer": "https://login.microsoftonline.com/<tid>/v2.0",
     "subject": "<app client id>", "agent_id": "agent-…"},
    {"kind": "mtls", "cn": "fib-sales-agent", "agent_id": "agent-…"}
  ]
}
```

- `agent_claims` is an **ordered** list; the first present claim is the
  caller's agent identity. For Entra client-credentials tokens use `azp`
  (v2 tokens) with `appid` (v1) as the fallback. Configure the app
  registration for v2 tokens so `iss` matches the `issuer` above.
- `user_claims` and `roles_claim` populate `identity_claims` and
  `identity_roles` on the decision, the same fields `governed_identity()`
  fills in-process.
- **A valid token is not enough.** The gateway resolves a verified identity to
  an `agent_id` only through `bindings`. No binding means deny.
- The response contains only this tenant's data. The same issuer configured
  under two tenants resolves within the gateway's own tenant only.

#### `POST /api/v1/gateway/bundles`

```json
{"agents": [{"agent_id": "agent-…", "if_none_match": "<digest or null>"}]}
```

```json
{
  "issued_at": 1789900000.0,
  "agents": {
    "agent-…": {
      "status": "ok",
      "digest": "…",
      "files": {"10-base.cedar": "…"},
      "vendor_scoped_resources": false,
      "observation_collection": {"saturated_buckets": []}
    },
    "agent-other": {"status": "out_of_scope"}
  }
}
```

Each agent entry's fields match today's `GET /bundle` response, so the same
loader applies. `status` is one of:

| `status` | Meaning | Gateway behaviour |
|---|---|---|
| `ok` | Bundle present | Load atomically; on a bad bundle keep the previous generation |
| `not_modified` | `if_none_match` matched | Keep the cached bundle |
| `no_bundle` | Agent exists, no bundle yet | **Deny** that agent |
| `denied` | Operator revoked the agent | **Deny and drop the cached bundle** |
| `out_of_scope` | Agent is not in this gateway's tenant | **Deny**; log a warning |
| `unknown_agent` | No such agent | **Deny** |

A batch never fails as a whole because one agent is bad, and a response for
one agent never reveals whether an agent exists in another tenant:
`out_of_scope` is returned for both "other tenant" and "does not exist" when
the caller is not allowed to know the difference.

#### Attributing telemetry, heartbeats and reviews

The control plane derives `agent_id` from the credential today. For a gateway
it comes from the payload and is **checked against the gateway's tenant**:

- **OTLP:** every span and log record carries the attribute
  `parapetai.agent_id`. Records naming an out-of-scope agent are rejected
  through OTLP `partial_success`; the rest are accepted.
- **Heartbeat:** `{"pep_id", "version", "mode", "agents": [{"agent_id",
  "policy_generation", "bundle_digest"}]}`. One fleet row per gateway/agent.
- **Reviews:** `agent_id` in the body. The existing content fingerprint already
  binds an approval to one agent and one exact call.
- **Observation buckets:** keyed by the **verified** agent id, never the path
  claim.

### Failure semantics

The control plane is never on the decision path (invariant 6). These rules
are what keep an outage from changing a decision:

| Situation | Result |
|---|---|
| Control plane unreachable | Keep each agent's last bundle; **never** enable an agent the gateway has not loaded before |
| Identity config missing, or JWKS unreachable with no cached keys | Deny |
| Verified identity with no binding | Deny |
| Gateway secret revoked | All calls fail; the gateway keeps enforcing cached bundles but cannot approve reviews or refresh |
| Identity/binding change | Effective at the next config poll (same interval as bundles, 30 s default) |

### Standalone mode

With no control plane, the identity provider comes from
`PARAPETAI_IDP_ISSUER`, `PARAPETAI_IDP_JWKS_URL` and `PARAPETAI_IDP_AUDIENCE`,
and bindings from the JSON file named by `PARAPETAI_IDENTITY_BINDINGS` (the same record
shape as `bindings` above). This standalone mode is implemented today; see
[Gateway verified identity](reference/gateway-identity.md). Local mode is
single-tenant by construction.

## Running without a control plane

Everything above is optional. Point the SDK at local Cedar files and it enforces
with no network at all:

```python
from parapetai_agent import build_middleware
mw = build_middleware(policy_dir="./policies")   # no control_plane_url / secret
```

Bundle pull, heartbeat, and remote audit simply don't run. Decisions can still
be exported to any OTLP endpoint you configure yourself.
