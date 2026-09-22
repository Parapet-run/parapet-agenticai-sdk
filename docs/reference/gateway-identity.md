# Gateway: verified caller identity

By default the gateway takes the caller's identity from the URL:
`/a/{agent_id}/…` sets the Cedar principal, and nothing checks that the caller
is really that agent. That is fine inside one pod's trust boundary and wrong
anywhere else. **Verified identity** replaces the URL claim with a credential the
gateway actually checks.

It is **off unless configured**. With none of the settings below, the gateway
behaves exactly as it always has.

## What it does

A request can carry one of three credentials:

| Method | Proves | Where it is read from |
|---|---|---|
| **mTLS** | The caller holds a private key for a certificate your CA signed | The TLS handshake, terminated in the gateway itself |
| **JWT** | Your IdP (Entra, Okta, …) issued this token to this application | The `X-Parapetai-Identity` header |
| **Shared secret** | The caller holds a per-agent bearer secret the gateway knows the hash of | The `X-Parapetai-Identity` header, same as JWT |

mTLS is the default and strongest method (a private key, never transmitted).
Shared secret exists only for a caller that can present neither a client
certificate nor an IdP token — see [Shared secret](#shared-secret-additive-off-by-default) below;
it is strictly additive and off by default.

Either way, proof of *who the caller is* is not enough to pick a policy. A
**binding** maps that identity to a Parapet `agent_id`, and only a binding says
which agent it is:

```json
[
  {"kind": "jwt",  "issuer": "https://login.microsoftonline.com/<tenant>/v2.0",
   "subject": "<application (client) id>", "agent_id": "fib-sales-agent"},
  {"kind": "mtls", "cn": "fib-hr-agent", "agent_id": "fib-hr-agent"}
]
```

The Cedar principal is then `Agent::"fib-sales-agent"` (the bound agent), never
whatever the token or URL said.

## Rules the gateway enforces

| Situation | Result |
|---|---|
| No credential | Path claim, as before (or `401` if `PARAPETAI_REQUIRE_VERIFIED_IDENTITY=true`) |
| A credential is presented but invalid | **`401`.** It never falls back to the path claim: garbage must not be a way to downgrade |
| Valid, but no binding for it | **`403`** `identity_not_bound` |
| Valid, but the URL names a different agent | **`403`** `identity_path_mismatch` |
| mTLS and a header credential (JWT or secret) both valid, different agents | **`403`** `identity_conflict` |
| Certificate has no CN, or more than one | `401` (there is no defensible "which one") |
| A shared secret that isn't a bound one, or is garbage | **`401`** `invalid_secret` (never `403`: there is no separate "verify, then check binding" step for a secret, so wrong and unbound are the same case) |

The response never says *why* a token failed (expired, bad signature, wrong
audience): that goes to the gateway's log, so a caller can't use it to probe.

A binding to the reserved id `anonymous` is rejected at startup, as is an
identity bound to two agents, and any `agent_id` with characters that could
alter a Cedar principal expression.

## JWT (Entra)

```bash
export PARAPETAI_IDP_ISSUER="https://login.microsoftonline.com/<tenant-id>/v2.0"
export PARAPETAI_IDP_JWKS_URL="https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys"
export PARAPETAI_IDP_AUDIENCE="api://parapet-gateway"
export PARAPETAI_IDENTITY_BINDINGS=/etc/parapetai/bindings.json
```

- Register the gateway as an API in Entra and use its Application ID URI as the
  audience. Configure the API for **v2 tokens** so `iss` matches the issuer
  above (v1 tokens use `sts.windows.net`).
- The agent identity is the first present claim of `azp` (v2), `appid` (v1),
  `client_id` (RFC 9068). Override with `PARAPETAI_IDP_AGENT_CLAIMS`.
- The caller sends `X-Parapetai-Identity: Bearer <token>` (a bare token also
  works). It is **stripped before the request is forwarded upstream**.
- `roles` and `oid`/`tid` populate `identity_roles` / `identity_claims` exactly as
  [`governed_identity()`](governed-identity.md) does in-process, so a
  role-gated Cedar policy behaves the same through the gateway. Cedar policy
  can also branch on `context.identity_method` (`jwt`, `mtls`, `mtls+jwt`, or
  `path` for an unverified request).

Verification is strict: the signature is checked against the IdP's published
keys, and `iss`, `aud` and `exp` are all mandatory. Only asymmetric algorithms
are accepted (default `RS256`); `none` and `HS*` cannot even be configured, which
closes the algorithm-confusion attack.

### Why not the `Authorization` header?

Under the default `passthrough` credential mode, `Authorization` carries the
caller's **upstream** credential: an OpenAI key, or the token for a downstream
MCP server. That is not an identity token, and verifying it as one would reject
every LLM call. So the identity token has its own header.

If you want the standard header (for an MCP-only deployment where the gateway is
the only bearer consumer), set `PARAPETAI_IDENTITY_HEADER=authorization`. Then
the downstream server no longer receives the caller's token, so use
`PARAPETAI_CREDENTIAL_MODE=broker`. It cannot be combined with
`PARAPETAI_MCP_AUTH_MODE=oauth2`, which also reads that header; the gateway
refuses to start.

### Key handling

Keys come only from the IdP's key set, never from the token. The fetch is
HTTPS-only and rate-limited (30 s between refetches) so a flood of tokens with
made-up `kid`s can't turn the gateway into a request amplifier against your IdP.
If the IdP is unreachable, previously fetched keys keep working for up to 24
hours, then verification fails closed. A key the gateway has never seen is never
trusted on the strength of an outage.

## mTLS

```bash
export PARAPETAI_TLS_CERT=/etc/parapetai/tls/server.crt
export PARAPETAI_TLS_KEY=/etc/parapetai/tls/server.key
export PARAPETAI_TLS_CLIENT_CA=/etc/parapetai/tls/client-ca.crt
export PARAPETAI_TLS_CLIENT_AUTH=required     # or: optional
export PARAPETAI_IDENTITY_BINDINGS=/etc/parapetai/bindings.json
```

Setting the client CA turns mTLS on. The certificate's **CN** is the identity.

The certificate is read off the live TLS connection, after the handshake has
validated it against your CA. It is never read from a header: a caller cannot
supply `X-Client-Cert` or `X-Forwarded-Client-Cert` to impersonate an agent, and
the tests send exactly those and confirm they are ignored.

- `required` refuses any handshake with no client certificate. That also refuses
  a Kubernetes or Docker probe, which carries none: give probes their own port
  with `PARAPETAI_HEALTH_PORT` (see [Deployment](#deployment-what-to-expose)).
- `optional` lets JWT-only callers share the port. A caller with no certificate
  is unauthenticated, and is refused if verification is required.
- Behind a TLS-terminating ingress (a managed container platform's HTTP ingress, most L7 load
  balancers) the gateway sees plain HTTP and mTLS is unavailable. Use the JWT
  method there. Do not forward a CN in a header and trust it.

## Shared secret (additive, off by default)

```bash
export PARAPETAI_ALLOW_SHARED_SECRET=true
export PARAPETAI_IDENTITY_BINDINGS=/etc/parapetai/bindings.json
```

For a caller that can present neither a client certificate nor an IdP token —
a desktop agent whose MCP client only supports setting a request header, not
mTLS or a proxy. **Never a replacement for mTLS**: `PARAPETAI_TLS_CLIENT_AUTH`
still defaults to `required`, and turning this on has no effect for a caller
with no client certificate until that is also relaxed to `optional` (the
gateway logs `shared_secret_unreachable` at startup if you set this while
`required` is still in effect, since no such caller could ever reach it).

A binding looks like the others, with a `secret_hash` instead of a `cn` or
`issuer`/`subject`:

```json
{"kind": "secret", "agent_id": "fib-sales-agent",
 "secret_hash": "…sha256 hexdigest, 64 hex characters…"}
```

Mint the secret and compute the hash with `generate_secret()`/`hash_secret()`
(`parapetai_agent.agent_secrets` — the same functions the control plane's own
agent provisioning uses, so a secret's hash is computed identically on both
sides regardless of which one issued it). The secret is shown once; only its
hash is ever stored. The caller sends it as `Bearer <secret>` in the identity
header — **never `Authorization`**, which in passthrough credential mode
carries the caller's own upstream credential and must reach the downstream
server unexamined.

There is no separate "verify, then look up the binding" step the way mTLS/JWT
have one: the secret's hash *is* the lookup key, so a wrong or garbage secret
and an unbound one are indistinguishable — both are `401 invalid_secret`.

A single header value is only ever tried as one of JWT or shared secret,
picked by its shape (a JWT always has exactly two `.` characters; a generated
secret never does) — a well-formed JWT is never reinterpreted as an opaque
secret, and vice versa, even when both methods are enabled on the same
gateway.

## Rotation and reload

The gateway re-reads the mTLS files every `PARAPETAI_TLS_RELOAD_INTERVAL_S`
seconds (default 30) and, when their **content** changes, swaps in new material
for new connections. No restart, and it covers both the server certificate it
presents and the client CA it trusts. It compares content, not modification
times, because a secrets mount (a secrets-store CSI driver, a
Kubernetes Secret volume) replaces files by repointing a `..data` symlink.

- **A bad rotation never empties trust.** A truncated file, a key that does not
  match its certificate, or an empty CA makes the *new* material fail to build.
  That is logged (`tls_reload_failed_keeping_previous`) and the previous
  material keeps serving; fixing the files retries automatically. Startup is the
  opposite: unusable material at boot stops the gateway, so it can never come up
  without client verification.
- **Rotation is not revocation.** A connection that finished its handshake before
  the swap keeps the identity it authenticated with until it closes. Dropping a
  CA stops *new* handshakes from it at once, not connections already open. Bound
  that with short certificate lifetimes, not with reload.
- To rotate a client CA without breaking agents, publish a bundle holding the
  old **and** the new CA, move agents to certificates from the new one, then
  publish the new CA alone.

## Deployment: what to expose

An internet-facing gateway has up to two listeners, and they must not be confused:

| Listener | Setting | TLS | Expose it? |
|---|---|---|---|
| Agent-facing | `PARAPETAI_PORT` | mTLS (`PARAPETAI_TLS_*`) | Yes: this is the one a load balancer maps |
| Health | `PARAPETAI_HEALTH_PORT` | plain HTTP, no client cert | **No.** Orchestrator probes only |

The health listener answers `/__parapetai/health` and `/ready` with up/down and
nothing else: no policy digest, no generation, no paths, no proxying, and every
other path is a 404. Even so, do not put it behind a Service, load balancer or
ingress; a kubelet reaches a pod directly and needs none of them.

The control plane never calls either listener. The gateway *pulls* bundles and
*pushes* heartbeats and telemetry; nothing about the control plane's operation
requires an inbound path to the gateway.

Set `PARAPETAI_ADMIN_ROUTES=false` on any deployment whose agent-facing port is
reachable by more than the pod: the default-on `/__parapetai/policies`,
`/policies/reload` and `/observations` routes are unauthenticated and expose the
policy digest, its directory, and recent decisions with agent ids.

## What the control plane shows

A gateway authenticates to the control plane as an **agent** and heartbeats like any
other policy enforcement point, so it appears in the fleet under that agent. There is no
separate "gateway" identity in the protocol. With a control plane configured it also
sends a status block with each heartbeat (see
[the protocol](../CONTROL_PLANE_API.md#optional-details-on-the-heartbeat)):

- **TLS state:** the server certificate it is serving (subject, issuer, serial, expiry),
  the client CAs it trusts, how many rotations it has applied, and the last rotation
  error, if any.
- **Rotation events:** each reload, and each *rejected* reload with its reason (the
  previous certificate keeps serving).
- **Which agents are connected:** each identity seen recently, how it was identified
  (`mtls`, `jwt`, or `path` for an unverified URL claim), and request, denied and held
  counts.
- **Refused credentials**, counted by reason only. The callers are unverified, so none
  are identified.

The block is bounded and content-free. It identifies agents by the id they were bound to
and never carries a request, a prompt, a tool argument, a token, or key material.
`PARAPETAI_GATEWAY_SITE` labels where a gateway runs so one control plane can tell several
apart. Initiating a certificate rotation is done where the certificates live (your secrets
manager); the control plane shows the result, it does not hold the keys.

## Requiring verification

```bash
export PARAPETAI_REQUIRE_VERIFIED_IDENTITY=true
```

With this set, a request with no verified identity is refused with `401`. The
gateway's own `/__parapetai/*` endpoints are not governed requests and are
unaffected. The gateway will not start with this set and no method configured,
or with a method configured and no bindings: either would look secured while
being unusable.

## Limits (today)

- Bindings come from a local file (`PARAPETAI_IDENTITY_BINDINGS`), loaded at
  startup. Delivering them, and the IdP settings, from the control plane per
  tenant is the proposed [multi-agent gateway API](../CONTROL_PLANE_API.md#multi-agent-gateway-api-proposed).
- One IdP per gateway process.
- One policy set per gateway process. Verified identity chooses the Cedar
  *principal*; giving each agent its own bundle needs the control-plane work above.
- The bound `agent_id` is the Cedar principal (`Agent::"fib-sales-agent"`). The
  in-process SDK derives its principal from the token's own client id. If you
  share policy files between the two, bind agents to the ids your policies use.
