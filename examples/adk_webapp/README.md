# adk_webapp -- Verified end-user identity for a governed ADK web agent

Answers a question that took real investigation to answer correctly:
**where does ADK read end-user identity from, for a web deployment?**
Short version -- nowhere, by default. Confirmed live against `google-adk`'s
own source, not assumed:

- `Session.user_id` is a plain, UNVERIFIED string every `run_async()` call
  must supply -- ADK never authenticates it (it's part of the session
  storage lookup key in `runners.py`, not a checked credential).
- `adk web`'s own REST endpoints are unauthenticated by design (its own
  CLI docstring: *"run it on a trusted network only... do not expose it
  to untrusted or public networks"*) -- `user_id` there is a bare,
  uncontrolled URL path segment.
- Gemini Enterprise's own connector/`temp:<AUTH_ID>` credential injection
  (see e.g. [google/adk-samples' `adk-ae-oauth`](https://github.com/google/adk-samples/tree/main/python/agents/adk-ae-oauth))
  is a DIFFERENT thing -- a tool's own delegated credential for calling a
  third-party API (Google Drive, GitHub) on the user's behalf, not a
  signal about who's calling *your* agent.

The one place verified identity genuinely can originate, for a web
deployment, is the HTTP layer -- before ADK ever sees the request. This
example wires
[`parapetai_agent.identity_middleware.IdentityMiddleware`](../../src/parapetai_agent/identity_middleware.py)
(previously only demonstrated by [`maf_webapp/`] (in the private control-plane repo))
onto a real ADK-governed FastAPI app, and proves -- with real HTTP
requests through the real middleware chain, not just reasoning about it
-- that a verified JWT's claims survive all the way into `ParapetPlugin`'s
Cedar decisions.

See [`adk_sample_01/`](../adk_sample_01/README.md) for the CLI-shaped
answer instead (a single-operator script, where `Session.user_id` being
unverified doesn't matter the way it does here).

## The wiring is two lines

```python
app.add_middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())

runner = GovernedRunner(agent=root_agent, app_name=..., session_service=...)
```

`IdentityMiddleware` lifts whatever `Authorization: Bearer <jwt>` arrives
on a request into ambient identity for the duration of that request --
every governed call made anywhere inside the handler picks it up
automatically via the same `parapetai_agent.scoped_data` contextvar
`governed_identity()` uses, no `with governed_identity(...):` needed at
the call site. `GovernedRunner` is exactly what `adk_sample_01/` uses,
with one difference worth being explicit about:
**`trust_session_user_id` is left at its default, `False`.** That sample
opts it on (nothing to protect there); this one doesn't need to, because
it now has a real, verified identity source -- see
[`parapetai_agent/adk.py`](../../src/parapetai_agent/adk.py)'s
own module docstring for why mixing an unverified fallback into an app
that already has a verified source would be the wrong default.

## Policy: `policies/30-identity.cedar`

This app's `lookup_order` tool is governed by the SAME shared, real Cedar
rule `governed_maf_demo.py` (MAF) demonstrates -- `lookup_order` requires
the `OrderViewer` role, gated on `context has identity_roles` so a caller
that asserts NO identity at all is unaffected (rule doesn't apply), and
only a caller that DOES assert roles, but lacks this one, is denied. One
policy, enforcing the same thing across both framework integrations this
package ships.

## Run

```bash
cp examples/adk_webapp/.env.example examples/adk_webapp/.env
# edit .env: fill in GOOGLE_API_KEY
uv run --extra adk --extra web python3 examples/adk_webapp/web_app.py
```

In another shell, mint a few demo tokens (see
[`mint_demo_jwt.py`](mint_demo_jwt.py)'s own docstring for why an
*unsigned* JWT is an appropriate fixture here specifically, and when it
wouldn't be):

```bash
TOKEN_OV=$(python3 examples/adk_webapp/mint_demo_jwt.py --sub alice --roles OrderViewer)
TOKEN_GUEST=$(python3 examples/adk_webapp/mint_demo_jwt.py --sub bob --roles Guest)
```

Three scenarios, same tool, same policy, different identity:

```bash
# 1. No Authorization header at all -> no identity asserted -> the
#    OrderViewer rule doesn't apply -> tool call ALLOWED.
curl -s -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" \
  -d '{"user_id": "anon", "message": "Look up order 12345"}' | python3 -m json.tool

# 2. A verified token WITHOUT the OrderViewer role -> the rule applies and
#    the required role is missing -> tool call DENIED
#    (tool_denied: true in the response -- see track_tool_denials() in
#    web_app.py for why the response checks this deterministically
#    instead of pattern-matching the model's own reply text).
curl -s -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN_GUEST" \
  -d '{"user_id": "bob", "message": "Look up order 12345"}' | python3 -m json.tool

# 3. A verified token WITH the OrderViewer role -> ALLOWED.
curl -s -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN_OV" \
  -d '{"user_id": "alice", "message": "Look up order 12345"}' | python3 -m json.tool
```

Watch `examples/adk_webapp/logs/parapetai-decisions.jsonl` while you run
these -- each `tool_call` decision's `context.identity_claims`/
`identity_roles` shows exactly what `IdentityMiddleware` extracted from
that request's token (or didn't, for scenario 1).

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, then copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID`
block its agent detail page prints ("Integrating this agent") into `.env`.
This app then pulls that agent's real policy bundle instead of the
bundled default, and pushes decision audit events back to the control
plane.
