# Deploying parapetai-gateway to Azure

A single, standalone Azure Container App running `gateway/Dockerfile`. This
is the "run the gateway anywhere" path from the main `gateway/README.md` --
these scripts are Azure-specific, but the image itself is not: the same
container runs unmodified on Cloud Run, Fargate, or a bare VM with a
different set of env vars.

**One gateway deployment can front an arbitrary set of downstream MCP
servers**, not just one -- it is a reverse proxy, not itself an MCP server.
`PARAPETAI_MCP_BASE_URL` (single target, reachable at bare `/a/<agent-id>/mcp`)
and `PARAPETAI_MCP_UPSTREAMS` (a JSON name -> URL map, each reachable at
`/a/<agent-id>/mcp/<name>`) are two different ways to configure this -- see
`.env.example`'s own comments for the exact shape, and step 8 below for
registering more than one with Atlassian Rovo.

**No local Cedar policy ships with this deployment.** `PARAPETAI_CONTROL_PLANE_URL`
/ `PARAPETAI_AGENT_ID` / `PARAPETAI_AGENT_SECRET` are required, not optional
-- `server/main.py` fetches the policy bundle from the control plane
synchronously, before the `PolicyEngine` is even constructed, specifically
so no `.cedar` file needs to be baked into the image or committed to this
directory. Every restart re-fetches from the control plane; there is no
local fallback policy to fall back to.

**Stateless, single-replica.** Unlike `parapet-platform`'s control-plane/
maf-webapp deployments, there's no Azure Files share here -- nothing this
container writes needs to survive a restart. `GATEWAY_MAX_REPLICAS` defaults
to 1 in `config.sh` because of one thing that genuinely is in-memory and
replica-local: `mcp_oauth.py`'s OAuth client/code/token registry (when
`PARAPETAI_MCP_AUTH_MODE=oauth2`). A DCR registration against one replica
would 401 if a later request load-balanced to a different one. Raise the
replica count only after that state moves to a shared store.

## Prerequisites

- `az` CLI, logged in (`az login`) with a subscription selected
  (`az account set --subscription ...`).
- A Parapet control-plane agent already provisioned (dashboard, or
  `parapetai-mcp`'s `parapet_provision_agent` tool) -- you need its
  `agent_id`/`agent_secret` before step 2 below.
- The real, already-running MCP server you want this gateway to front,
  reachable from Azure (not `localhost`).

## Steps

1. **Configure names** (optional). `config.sh` has real, chosen defaults
   (`parapet-gateway-rg`, `parapetgatewayacr`, ...) -- edit it directly, or
   export overrides before running anything. `ACR_NAME` must be globally
   unique across all of Azure if you change it.

2. **Set secrets and endpoints.**
   ```bash
   cp .env.example .env
   # edit .env -- see its own comments for what each value means
   ```
   At minimum: `PARAPETAI_CONTROL_PLANE_URL`, `PARAPETAI_AGENT_ID`,
   `PARAPETAI_AGENT_SECRET`. Leave `PARAPETAI_MCP_AUTH_MODE=none` and
   `PARAPETAI_MCP_BASE_URL` unset for a first smoke-test deploy (step 5);
   fill both in once you're ready to front a real MCP server (step 6).

3. **Provision shared infrastructure** (resource group, ACR, Container Apps
   Environment):
   ```bash
   ./provision.sh
   ```

4. **Deploy:**
   ```bash
   ./deploy-gateway.sh
   ```
   Prints the app's URL and, depending on `PARAPETAI_MCP_AUTH_MODE`, either
   the bare MCP endpoint or the OAuth discovery URLs to check.

5. **Verify the deployment itself, before wiring up anything real:**
   ```bash
   curl https://<gateway FQDN>/__parapetai/ready
   ```
   `"status": "ready"` confirms the control-plane bundle fetch succeeded and
   Cedar has real policy loaded -- `"no policies"` (503) means the fetch
   failed and there's nothing to enforce (check `PARAPETAI_AGENT_SECRET` and
   that the control plane is reachable from this container).

6. **Point `PARAPETAI_MCP_BASE_URL` at your real MCP server**, if you
   haven't already, and re-run `./deploy-gateway.sh` (safe to re-run -- it's
   `az containerapp update` against the same app). Confirm a call reaches it:
   ```bash
   curl -X POST https://<gateway FQDN>/a/<agent-id>/mcp \
     -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"<a real tool>","arguments":{}}}'
   ```
   A policy-denied tool comes back as a JSON-RPC error (code `-32000`), not
   an HTTP-layer failure -- see `server/app.py`'s `_provider_shaped_block`.

7. **Switch on OAuth 2.1 + DCR before registering with Atlassian Rovo.**
   Rovo's "add an external MCP server" flow requires this
   ([support.atlassian.com](https://support.atlassian.com/organization-administration/docs/add-an-external-mcp-server-from-atlassian-administration/)):
   HTTPS, Streamable HTTP transport, OAuth 2.1 authorization_code+PKCE, and
   Dynamic Client Registration (RFC 7591). This gateway already serves plain
   HTTPS + Streamable HTTP once deployed (Container Apps terminates TLS; the
   `/mcp` route relays both the JSON-RPC POST and the SSE-opening GET
   untouched -- see `server/app.py`'s stream-detection comment). What's
   gated behind a mode switch is the OAuth/DCR layer itself:
   ```bash
   # in .env:
   PARAPETAI_MCP_AUTH_MODE=oauth2
   PARAPETAI_MCP_OAUTH_SHARED_SECRET=$(openssl rand -base64 32)
   ```
   Re-run `./deploy-gateway.sh`. The gateway fails closed at startup if
   `oauth2` mode is set without a shared secret (`mcp_oauth.py`'s module
   docstring explains why this secret is sufficient -- it's a one-time
   deployment-operator gate on the `/authorize` consent step, not a
   per-user identity system; Cedar remains the actual authorization
   decision either way).

   Verify the OAuth surface before touching Atlassian at all:
   ```bash
   curl https://<gateway FQDN>/.well-known/oauth-protected-resource
   curl https://<gateway FQDN>/.well-known/oauth-authorization-server
   curl -X POST https://<gateway FQDN>/mcp   # expect 401 + WWW-Authenticate
   ```

8. **Register with Atlassian Rovo** (org admin panel, not this repo):
   Admin Hub -> Apps -> Sites -> your site -> Connected apps -> dropdown next
   to "Explore apps" -> **Add external MCP server** -> accept the disclaimer
   -> **Custom MCP server** -> enter:
   ```
   https://<gateway FQDN>/a/<agent-id>/mcp
   ```
   (or `.../mcp/<target-name>` per entry in `PARAPETAI_MCP_UPSTREAMS`, if you
   configured more than one downstream server -- **repeat this whole step
   once per target**, since Atlassian treats each URL as a separate external
   MCP server with its own discovery/DCR/token flow; they all share this
   gateway's one `PARAPETAI_MCP_OAUTH_SHARED_SECRET` consent prompt and one
   Cedar policy set, just evaluated with a different `context.mcp_target`
   per call.)

   Atlassian's client will discover `/.well-known/oauth-protected-resource`
   from the 401, follow it to the authorization-server metadata, POST to
   `/register` (DCR), then run the `/authorize` + `/token` exchange --
   you'll be prompted once per target for `PARAPETAI_MCP_OAUTH_SHARED_SECRET`
   on the `/authorize` consent page. From there, every tool call Rovo makes
   is a Cedar decision on this gateway before it ever reaches the real MCP
   server for that target. See this conversation's earlier research for the
   full Atlassian-
   side context and citations (community reports that wiring a custom MCP
   server into one *specific* Rovo Studio agent, vs. the org-wide external
   MCP registration above, may need more than the Studio UI alone --
   confirm current behavior with your Atlassian admin before relying on it).

## Redeploying after a code change

Re-run `./deploy-gateway.sh` -- safe to run repeatedly (`az containerapp
update` if the app already exists, with a revision suffix keyed to the
image's content digest so a rebuild with no other config change still
rolls out).

## Fleet visibility

Each running gateway instance heartbeats to the control plane's Fleet table
under its own `pep_id` (defaults to `pep-<hostname>-<pid>`, i.e. distinct per
container revision/replica), while all instances share one `agent_id`/
`agent_secret` -- the Cedar principal and policy this gateway enforces. With
`GATEWAY_MAX_REPLICAS=1` (the default -- see this file's header) you'll see
exactly one Fleet row per running revision.

## Cost

`GATEWAY_MIN_REPLICAS` defaults to 1 (not 0): unlike `parapet-platform`'s
scale-to-zero control-plane/maf-webapp deployments, this gateway is meant to
sit in front of live, synchronous tool calls from Rovo -- a cold start
(container pull + bundle fetch + uvicorn boot) mid-OAuth-handshake or
mid-tool-call is worse here than the small always-on compute cost. Set
`GATEWAY_MIN_REPLICAS=0` yourself if your use case tolerates the latency.
