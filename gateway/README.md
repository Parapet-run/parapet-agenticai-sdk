# parapetai-gateway

**The Parapet PEP for agents you can't modify.** Point your agent's provider
base URL at it and every model call and tool call becomes a
[Cedar](https://www.cedarpolicy.com/) policy decision — default-deny,
fail-closed, content-free audit — before it reaches the provider.

Same enforcement engine as [`parapetai-agent`](../README.md), reached a
different way. Embed the SDK when you can change the app; run this when you
can't, or when the agent isn't Python at all.

```bash
uvx parapetai-gateway                 # local dev, no Docker
```

```bash
docker build -t parapetai-gateway .   # production
docker run -p 8080:8080 parapetai-gateway
```

Then, in the app you want governed — **no code change**:

```bash
export OPENAI_BASE_URL=http://localhost:8080/a/<agent-id>/v1
```

That's the whole integration (see `docs/adr/0002-base-url-over-mitm.md`). The
`/a/{agent_id}` prefix sets the Cedar principal; omitting it evaluates as
`Agent::"anonymous"`, still under default-deny — never a bypass.

## What it does

Provider-shaped endpoints, so unmodified SDKs work untouched: OpenAI,
Anthropic, Gemini, and MCP (`tools/call`). A denial comes back in the
provider's *own* error shape, so a client SDK surfaces a readable governance
message instead of a deserialisation failure. Streaming relays faithfully —
SSE chunks are never buffered or reordered.

## Credentials

`PARAPETAI_CREDENTIAL_MODE` defaults to `passthrough`: the caller's own
upstream key rides through unchanged and the gateway never holds a provider
credential. `broker` (opt-in) strips it and injects a gateway-held key
instead. Know which is active before reasoning about where a key can leak —
see `docs/adr/0003`.

## Policy

Point it at a local Cedar directory, or at a control plane
(`PARAPETAI_CONTROL_PLANE_URL` / `PARAPETAI_AGENT_SECRET`) to pull a signed
bundle and keep it refreshed. Decisions are always evaluated **locally** — the
control plane is never on the request path.

MIT licensed.
