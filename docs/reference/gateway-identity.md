# Gateway: verified caller identity

By default the gateway takes the caller's identity from the URL:
`/a/{agent_id}/…` sets the Cedar principal, and nothing checks that the caller
is really that agent. That is fine inside one pod's trust boundary and wrong
anywhere else. **Verified identity** replaces the URL claim with a credential the
gateway actually checks.

It is **off unless configured**. With none of the settings below, the gateway
behaves exactly as it always has.

## What it does

A request can carry one of two credentials:

| Method | Proves | Where it is read from |
|---|---|---|
| **mTLS** | The caller holds a private key for a certificate your CA signed | The TLS handshake, terminated in the gateway itself |
| **JWT** | Your IdP (Entra, Okta, …) issued this token to this application | The `X-Parapetai-Identity` header |

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
| mTLS and JWT both valid, different agents | **`403`** `identity_conflict` |
| Certificate has no CN, or more than one | `401` (there is no defensible "which one") |

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
  a Kubernetes probe that carries none: probe the port with a client certificate,
  or use `optional`.
- `optional` lets JWT-only callers share the port. A caller with no certificate
  is unauthenticated, and is refused if verification is required.
- Behind a TLS-terminating ingress (Azure Container Apps, most L7 load
  balancers) the gateway sees plain HTTP and mTLS is unavailable. Use the JWT
  method there. Do not forward a CN in a header and trust it.

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
