"""Gateway PEP.

Exposes provider-shaped endpoints so unmodified SDKs can be pointed here with a
single env var. Evaluates Cedar, then forwards upstream.

Caller identity comes from a /a/{agent_id} path prefix, not a token -- see
parapetai_agent.identity (the open, shared foundation this gateway and
parapetai-agent both depend on). Credential handling is mode-gated:
PARAPETAI_CREDENTIAL_MODE defaults to passthrough (the caller's own upstream
credential rides through unchanged); see parapetai_gateway.config and docs/adr/0003.

Streaming is a first-class path: every listed framework streams by default, and
buffering an SSE response breaks them. We relay chunks as they arrive.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from parapetai_agent import governance_runtime
from parapetai_agent.control_plane import ReviewClient, review_fingerprint
from parapetai_agent.identity import Caller, resolve_from_path
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.providers.parsers import Snapshot, parse_request
from parapetai_gateway import mcp_oauth
from parapetai_gateway.config import settings
from parapetai_gateway.fingerprint import fingerprint

# The header a held call comes back on, and the one a client re-presents to
# collect its approval. Named once: a typo in either direction would silently
# turn "approved and retried" into "held again", which looks like a queue that
# never drains rather than like a bug.
REVIEW_HEADER = "x-parapetai-review-id"

log = structlog.get_logger(__name__)
router = APIRouter()

# Dev/test surface for the conformance harness: proves a framework's traffic
# actually reached the gateway. Deliberately excludes message content and
# credentials -- see _record_observation.
_OBSERVATIONS_CAP = 500


def create_app(engine: PolicyEngine, reviews: ReviewClient | None = None) -> FastAPI:
    # Fail closed on a misconfigured oauth2 mode rather than silently running
    # an authorization server anyone can complete DCR + /authorize against
    # with no credential at all -- see mcp_oauth.py's module docstring.
    if settings.oauth_enabled and not settings.mcp_oauth_shared_secret:
        raise RuntimeError(
            "PARAPETAI_MCP_AUTH_MODE=oauth2 requires PARAPETAI_MCP_OAUTH_SHARED_SECRET to be set"
        )

    app = FastAPI(title="Parapet", docs_url="/__parapetai/docs", redoc_url=None)
    app.state.engine = engine
    # None when no control plane is configured -- the gateway then behaves
    # exactly as it did before approvals existed: a review is refused and
    # never queued, because there is no queue and so nobody to ask.
    app.state.reviews = reviews
    app.state.http = httpx.AsyncClient(timeout=settings.upstream_timeout)
    observations: deque[dict[str, Any]] = deque(maxlen=_OBSERVATIONS_CAP)
    app.state.observations = observations

    @app.on_event("shutdown")
    async def _close() -> None:
        await app.state.http.aclose()

    # Control endpoints are namespaced so they cannot collide with a provider path.
    @app.get("/__parapetai/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/__parapetai/ready")
    async def ready() -> Response:
        status = engine.status
        if status["policy_files"] == 0:
            return JSONResponse({"status": "no policies"}, status_code=503)
        return JSONResponse({"status": "ready", **status})

    @app.get("/__parapetai/policies")
    async def policies() -> dict[str, Any]:
        return engine.status

    @app.post("/__parapetai/policies/reload")
    async def reload_policies() -> dict[str, Any]:
        return engine.reload(force=True)

    # Read-only, no auth: a dev/test surface, not a governed provider route.
    @app.get("/__parapetai/observations")
    async def get_observations(agent_id: str | None = None) -> dict[str, Any]:
        records = list(observations)
        if agent_id is not None:
            records = [r for r in records if r["agent_id"] == agent_id]
        records.reverse()  # most recent first
        return {"records": records}

    if settings.oauth_enabled:
        _add_oauth_routes(app)

    app.include_router(router)
    return app


_AUTHORIZE_FIELDS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "state",
)


def _add_oauth_routes(app: FastAPI) -> None:
    """OAuth 2.1 authorization-code+PKCE + Dynamic Client Registration for
    the /mcp path -- see mcp_oauth.py's module docstring for what this is
    and, more importantly, what it deliberately is not. Registered only
    when PARAPETAI_MCP_AUTH_MODE=oauth2 so a "none"-mode gateway's route
    table is byte-for-byte what it always was."""

    def _base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource(request: Request) -> dict[str, Any]:
        base_url = _base_url(request)
        return mcp_oauth.protected_resource_metadata(base_url, f"{base_url}/mcp")

    @app.get("/.well-known/oauth-protected-resource/{resource_path:path}")
    async def oauth_protected_resource_for(resource_path: str, request: Request) -> dict[str, Any]:
        # RFC 8615 path-mapped well-known URI: with more than one downstream
        # MCP server registered as SEPARATE Atlassian "external MCP server"
        # entries (one per /a/{agent}/mcp/{target}), each does its own
        # discovery against its own path -- a single shared document
        # (the route above) would claim every target IS the bare /mcp
        # resource, which is wrong once more than one target exists.
        base_url = _base_url(request)
        return mcp_oauth.protected_resource_metadata(base_url, f"{base_url}/{resource_path}")

    @app.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server(request: Request) -> dict[str, Any]:
        return mcp_oauth.authorization_server_metadata(_base_url(request))

    @app.post("/register")
    async def oauth_register(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _oauth_error(
                mcp_oauth.OAuthError("invalid_client_metadata", "body must be JSON")
            )
        try:
            return JSONResponse(mcp_oauth.register_client(body), status_code=201)
        except mcp_oauth.OAuthError as exc:
            return _oauth_error(exc)

    @app.get("/authorize")
    async def oauth_authorize_get(request: Request) -> Response:
        return _render_consent_form(request)

    @app.post("/authorize")
    async def oauth_authorize_post(request: Request) -> Response:
        form = await request.form()

        def _field(name: str) -> str | None:
            # Every field here is a plain text input (see _render_consent_form)
            # -- never a file upload -- but FormData.get()'s return type covers
            # UploadFile too, so narrow it explicitly rather than trusting that.
            value = form.get(name)
            return value if isinstance(value, str) and value else None

        params = {name: _field(name) for name in _AUTHORIZE_FIELDS}
        secret = _field("secret") or ""
        try:
            mcp_oauth.start_authorization(
                client_id=params["client_id"] or "",
                redirect_uri=params["redirect_uri"] or "",
                code_challenge=params["code_challenge"],
                code_challenge_method=params["code_challenge_method"],
                response_type=params["response_type"],
            )
        except mcp_oauth.OAuthError as exc:
            return _oauth_error(exc)

        if not secrets.compare_digest(secret, settings.mcp_oauth_shared_secret or ""):
            return _render_consent_form(request, params=params, error="Incorrect secret.")

        code = mcp_oauth.issue_code(
            client_id=params["client_id"] or "",
            redirect_uri=params["redirect_uri"] or "",
            code_challenge=params["code_challenge"] or "",
            ttl_s=settings.mcp_oauth_code_ttl_s,
        )
        redirect_uri = params["redirect_uri"] or ""
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}"
        if params["state"]:
            location += f"&state={params['state']}"
        return Response(status_code=302, headers={"Location": location})

    @app.post("/token")
    async def oauth_token(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
        try:
            token = mcp_oauth.exchange_token(
                grant_type=body.get("grant_type"),
                code=body.get("code"),
                redirect_uri=body.get("redirect_uri"),
                client_id=body.get("client_id"),
                code_verifier=body.get("code_verifier"),
                ttl_s=settings.mcp_oauth_token_ttl_s,
            )
        except mcp_oauth.OAuthError as exc:
            return _oauth_error(exc)
        return JSONResponse(token, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _oauth_error(exc: mcp_oauth.OAuthError) -> JSONResponse:
    return JSONResponse(exc.to_body(), status_code=exc.status)


def _render_consent_form(
    request: Request, *, params: dict[str, Any] | None = None, error: str | None = None
) -> Response:
    """A single, deliberately minimal HTML page: this is not a login system,
    it's a one-shot check of the deployment operator's shared secret (see
    mcp_oauth.py's module docstring). GET populates the form from the
    client's query params; a failed POST re-renders it with the same values
    so the operator doesn't have to re-copy the client_id/redirect_uri by
    hand."""
    from html import escape

    q = params or dict(request.query_params)
    fields = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(str(v))}">'
        for k, v in q.items()
        if v is not None
    )
    error_html = f'<p style="color:#b00">{escape(error)}</p>' if error else ""
    body = f"""<!doctype html><html>
<body style="font-family:sans-serif;max-width:420px;margin:4rem auto">
<h3>Authorize MCP access</h3>
{error_html}
<form method="post" action="/authorize">
{fields}
<label>Shared secret<br><input type="password" name="secret" autofocus></label><br><br>
<button type="submit">Approve</button>
</form>
</body></html>"""
    return Response(content=body, media_type="text/html")


def create_app_factory() -> FastAPI:
    """Zero-arg factory for `uvicorn --factory` (the `make dev` hot-reload loop).

    uvicorn's reloader re-imports this module in a fresh subprocess and calls
    the factory with no arguments, so the engine must be built from
    env-driven settings here -- same construction as the production
    entrypoint in server/main.py, minus the policy-file watcher thread.
    """
    engine = PolicyEngine(settings.policy_dir, settings.entities_path)
    return create_app(engine)


def _mcp_target(path: str) -> str | None:
    """/mcp -> None (single-upstream, PARAPETAI_MCP_BASE_URL).
    /mcp/{target}[/...] -> "{target}" (routes via PARAPETAI_MCP_UPSTREAMS).

    Only the first segment after /mcp/ is the target name; anything past it
    is not used by MCPParser (a Streamable HTTP server has one endpoint, see
    Settings.mcp_upstream_for's docstring) but is tolerated rather than
    rejected, in case a caller's URL includes a trailing slash or similar.
    """
    rest = path[len("/mcp") :]
    if not rest or rest == "/":
        return None
    return rest.lstrip("/").split("/", 1)[0] or None


@router.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(full_path: str, request: Request) -> Response:
    engine: PolicyEngine = request.app.state.engine
    client: httpx.AsyncClient = request.app.state.http

    # UNAUTHENTICATED: agent_id is a caller-supplied claim, not a verified
    # credential -- see parapetai_agent.identity. Anonymous is still evaluated by Cedar
    # below, never a bypass.
    caller, path = resolve_from_path("/" + full_path)

    raw = await request.body()
    if len(raw) > settings.max_body_bytes:
        return _error(413, "body_too_large", "request exceeds PARAPETAI_MAX_BODY_BYTES")

    body: Any = None
    if raw and "json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None  # unparsed -> coarse action, not a bypass

    snapshot = parse_request(path, body)

    # The gateway fronts an ARBITRARY set of downstream MCP servers, not one
    # fixed one -- /mcp routes via PARAPETAI_MCP_BASE_URL (unchanged,
    # single-target); /mcp/{target} routes to whichever server
    # PARAPETAI_MCP_UPSTREAMS names "target" as. See config.Settings.
    # mcp_upstream_for()'s own docstring for the resolution rule.
    mcp_target = _mcp_target(path) if snapshot.provider == "mcp" else None

    # OAuth 2.1 is an authentication gate in front of Cedar, not a
    # replacement for it -- Cedar still evaluates every request below
    # exactly as it always has, keyed on the path-claimed agent_id, exactly
    # as unauthenticated-by-OAuth requests already are today. This check
    # exists only because some MCP clients (e.g. Atlassian Rovo's external
    # MCP server requirement) refuse to talk to a server that doesn't offer
    # it. See mcp_oauth.py's module docstring.
    if snapshot.provider == "mcp" and settings.oauth_enabled:
        if not mcp_oauth.validate_bearer(request.headers.get("authorization")):
            base_url = str(request.base_url).rstrip("/")
            # RFC 8615 path-mapped well-known URI: the resource metadata for
            # THIS exact resource (e.g. /a/agent/mcp/jira), not one shared
            # document for the whole origin -- required once more than one
            # target is registered as a SEPARATE Atlassian "external MCP
            # server", each doing its own discovery against its own path.
            metadata_url = f"{base_url}/.well-known/oauth-protected-resource{path}"
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
            )

    context = {
        **snapshot.to_context(),
        "method": request.method,
        "path": path,
        "tenant": caller.tenant,
        "trust_tier": caller.trust_tier,
    }
    if mcp_target is not None:
        # Lets Cedar gate access per downstream server, not just per tool
        # name within one server -- e.g. "this agent may reach jira but not
        # servicenow" is otherwise inexpressible when both share one gateway.
        context["mcp_target"] = mcp_target

    decision = engine.evaluate(
        principal=caller.principal,
        action=snapshot.action,
        resource=f'Resource::"{snapshot.provider}"',
        context=context,
    )

    client_name, client_version = fingerprint(request.headers)
    _audit(decision, caller, snapshot, context)
    if settings.log_prompts:
        _log_prompt_content(caller, snapshot, decision)
    _record_observation(
        request.app.state.observations,
        path=path,
        snapshot=snapshot,
        decision=decision,
        agent_id=caller.agent_id,
        client_name=client_name,
        client_version=client_version,
    )

    # Covers both "deny" and "review": Decision.allowed is False for each, and
    # neither reaches the upstream on its own. A review differs in that it can
    # be RESOLVED -- the caller gets a ticket on the 403 and re-presents it
    # once a human has approved (docs/adr/0009). Collection is attempted only
    # here, AFTER Cedar has already returned `review` for this exact request:
    # that ordering is what stops an approval unblocking a call that policy
    # has since hardened into a plain deny.
    #
    # Only when enforcing. In monitor mode nothing is blocked, so there is no
    # held call to approve and queueing one would fill an operator's queue with
    # requests that already went through.
    approved, review_id = False, None
    if not decision.allowed and settings.enforcing and decision.requires_review:
        approved, review_id = await _resolve_review(
            request,
            agent_id=caller.agent_id,
            path=path,
            snapshot=snapshot,
            decision=decision,
            raw=raw,
        )

    if not decision.allowed and not approved:
        if settings.enforcing:
            return _provider_shaped_block(snapshot.provider, decision, review_id=review_id)
        log.warning(
            "monitor_would_block",
            agent=caller.agent_id,
            action=snapshot.action,
            provider=snapshot.provider,
            effect=decision.effect,
            reason=decision.reason,
        )

    upstream = (
        settings.mcp_upstream_for(mcp_target)
        if snapshot.provider == "mcp"
        else settings.upstream_for(snapshot.provider)
    )
    if upstream is None:
        detail = f"mcp target {mcp_target!r}" if mcp_target else snapshot.provider
        return _error(502, "no_upstream", f"no upstream configured for {detail}")

    # MCP's Streamable HTTP transport opens its server->client notification
    # channel with a bare GET (no JSON body for MCPParser to read a `stream`
    # flag out of) -- distinct from every other provider, where `stream` is
    # always a body field on a POST. Buffering that GET would hold the
    # connection open until the client eventually disconnects, violating the
    # same "never buffer SSE" invariant this module already keeps for
    # OpenAI/Anthropic streaming.
    stream = snapshot.stream or (snapshot.provider == "mcp" and request.method == "GET")
    # Every mcp Upstream's base_url is already the complete downstream
    # endpoint, single-target or named (mcp_upstream_for()'s own docstring) --
    # the /a/{agent_id}/mcp[/{target}] prefix is a Parapet-only routing hint
    # the real server never sees, so nothing from `path` gets appended for
    # mcp at all. openai/anthropic/gemini are unaffected -- their base_url is
    # a host, and `path` selects a REST resource on it exactly as before.
    forward_path = "" if snapshot.provider == "mcp" else path
    return await _forward(client, upstream, forward_path, request, raw, stream)


# ── forwarding ───────────────────────────────────────────────────────


async def _forward(
    client: httpx.AsyncClient, upstream: Any, path: str, request: Request, raw: bytes, stream: bool
) -> Response:
    import os

    # passthrough (default): the caller's own Authorization/x-api-key header
    # rides through unchanged -- the gateway never sees a real provider
    # credential. broker (opt-in): strip it and inject PARAPETAI_<PROVIDER>_KEY
    # instead, as before. See docs/adr/0003.
    strip = _STRIPPED_HEADERS if settings.brokering_credentials else _CONNECTION_HEADERS
    headers = {k: v for k, v in request.headers.items() if k.lower() not in strip}
    if settings.brokering_credentials:
        credential = os.getenv(upstream.credential_env)
        if credential:
            headers[upstream.auth_header] = (
                f"Bearer {credential}" if upstream.auth_header == "Authorization" else credential
            )

    url = f"{upstream.base_url}{path}"
    req = client.build_request(
        request.method, url, headers=headers, content=raw, params=dict(request.query_params)
    )

    if not stream:
        try:
            resp = await client.send(req)
        except httpx.HTTPError as exc:
            log.error("upstream_error", url=url, error=str(exc))
            return _error(502, "upstream_error", str(exc))
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_passthrough_headers(resp.headers),
        )

    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        log.error("upstream_error", url=url, error=str(exc))
        return _error(502, "upstream_error", str(exc))

    # Read from the real response, not assumed: an MCP tools/call POST can
    # come back as a single application/json object OR as an SSE stream
    # depending on what the server chooses per-request (Streamable HTTP
    # transport), unlike OpenAI/Anthropic where stream=True always means SSE.
    # Hardcoding text/event-stream here mislabelled the JSON case.
    media_type = resp.headers.get("content-type", "text/event-stream").split(";")[0].strip()

    async def relay() -> AsyncIterator[bytes]:
        # Chunks are relayed untouched and in order. Do not accumulate: SSE
        # consumers time out, and reassembly here would add latency to every
        # token.
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(relay(), media_type=media_type, status_code=resp.status_code)


_CONNECTION_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
}
# broker mode only: also strip the caller's own credential so it can't leak
# upstream alongside the gateway-injected one.
_STRIPPED_HEADERS = _CONNECTION_HEADERS | {
    "authorization",
    "x-api-key",
    "x-goog-api-key",
}
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
}


def _passthrough_headers(headers: httpx.Headers) -> dict[str, str]:
    # content-encoding/content-length describe the wire body httpx already
    # transparently decompressed into resp.content -- forwarding them
    # unchanged mislabels the (larger, plain) body we actually send as still
    # being the original (smaller, gzipped) one, and the real client's own
    # decoder chokes on it. Real-world upstreams gzip non-trivial responses
    # (found via Cloudflare-fronted Groq); starlette recomputes a correct
    # content-length for the body we actually send.
    excluded = _HOP_BY_HOP | {"content-length", "content-encoding"}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


# ── responses ────────────────────────────────────────────────────────


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": code, "message": message}}, status_code=status)


# JSON-RPC implementation-defined server-error range is -32000..-32099, so
# both of these are spec-legal. A distinct code for a held call is the only
# way an MCP client can tell "never going to work, stop asking" from "a human
# is being asked, worth retrying" -- a single code would collapse exactly the
# distinction REVIEW exists to make.
_MCP_ERROR_DENY = -32000
_MCP_ERROR_REVIEW = -32001


def _review_fingerprint(agent_id: str, path: str, snapshot: Snapshot, raw: bytes) -> str:
    """Bind an approval to THIS request, bytes and all.

    The gateway hashes the raw body rather than parsed arguments. For a tool
    call the SDK's (tool, args) pair would do, but a model call carries its
    payload in the body, and a fingerprint over `action` alone would be
    identical for every model call to the same endpoint -- one approval would
    then unlock any later prompt. Hashing the bytes closes that, and makes the
    "approve a small request, retry with a bigger one" attack fail at the
    control plane, which compares the two fingerprints and refuses a mismatch.

    The body is hashed, never sent: a digest is not content (invariant 10).
    """
    return review_fingerprint(
        agent_id=agent_id,
        action=snapshot.action,
        tool_name=snapshot.tool_name,
        args={"path": path, "body_sha256": hashlib.sha256(raw).hexdigest()},
    )


async def _resolve_review(
    request: Request,
    *,
    agent_id: str,
    path: str,
    snapshot: Snapshot,
    decision: Decision,
    raw: bytes,
) -> tuple[bool, str | None]:
    """Collect an existing approval for this request, or queue a new one.

    Returns `(approved, review_id)`. `approved` is True only when this call
    just collected a grant for exactly these bytes -- the single thing that
    lets a held request through to the upstream.

    Called ONLY after Cedar has already returned `review` for this request, and
    that ordering is load-bearing: a grant can never unblock a hard `deny`, so
    if policy hardened between the approval and the retry the collection is
    never even attempted. The client re-presents the ticket rather than the
    gateway retrying on its own -- a proxy must not replay a non-idempotent
    request on the caller's behalf.

    ReviewClient is synchronous httpx, so both calls go through the threadpool;
    running them inline would block the event loop for every other in-flight
    proxied request while the control plane answers.
    """
    reviews: ReviewClient | None = request.app.state.reviews
    if reviews is None:
        return False, None

    fp = _review_fingerprint(agent_id, path, snapshot, raw)

    presented = request.headers.get(REVIEW_HEADER)
    if presented:
        granted = await run_in_threadpool(reviews.collect, review_id=presented, fingerprint=fp)
        if granted and granted.get("allowed"):
            log.info(
                "review_approved",
                agent=agent_id,
                review_id=presented,
                action=snapshot.action,
                path=path,
            )
            return True, presented
        # Not approved, mismatched, expired, or already spent. Fall through and
        # queue a FRESH review rather than echoing a ticket that will never
        # come good -- a client that retried with a different body must not be
        # handed back the id of the approval it just failed to use.
        log.warning("review_not_collectable", agent=agent_id, review_id=presented)

    # Tool arguments are what the policy matched on and are what an approver
    # needs to see. A model call's payload is the prompt, so it is never
    # previewed -- only fingerprinted above.
    preview = (
        json.dumps(snapshot.tool_args, sort_keys=True, default=str)[:2000]
        if snapshot.tool_name and snapshot.tool_args
        else None
    )
    body = await run_in_threadpool(
        reviews.submit,
        fingerprint=fp,
        tool_name=snapshot.tool_name,
        action=snapshot.action,
        policy_id=decision.determining_policies[0] if decision.determining_policies else None,
        reason=decision.annotations.get("review_reason") or decision.reason,
        risk_score=decision.annotations.get("risk_score"),
        args_preview=preview,
    )
    review_id = body.get("review_id") if body else None
    return False, (str(review_id) if review_id else None)


def _provider_shaped_block(
    provider: str, decision: Decision, review_id: str | None = None
) -> JSONResponse:
    """Return a non-allowed decision in the provider's own error shape.

    Covers a hard deny and a held-for-review call. SDKs parse errors
    strictly; a generic body produces a confusing deserialisation failure
    instead of a readable governance message.

    Both stay HTTP 403 deliberately. The request did not execute in either
    case, and every provider SDK already maps 403 onto its own permission
    error -- a 2xx (e.g. 202 Accepted) would make an OpenAI/Anthropic client
    try to deserialise a held call as a successful completion. The
    distinction rides on `x-parapetai-decision` and, for MCP, the JSON-RPC
    error code, where a client can act on it without the HTTP layer lying
    about whether it got a result.
    """
    review = decision.requires_review
    message = (
        f"Held for human approval by governance policy: {decision.reason}"
        if review
        else f"Blocked by governance policy: {decision.reason}"
    )
    if review and review_id:
        # The retry instruction belongs in the human-readable message too. A
        # 403 body is often the only thing an operator sees in an agent's logs,
        # and a bare ticket with no hint of what to do with it is a dead end.
        message += (
            f" (review {review_id} — once approved, retry this request with "
            f"{REVIEW_HEADER}: {review_id})"
        )
    headers = {
        "x-parapetai-decision": decision.effect,
        "x-parapetai-policy-generation": str(decision.policy_generation),
    }
    if review_id:
        headers[REVIEW_HEADER] = review_id

    payload: dict[str, Any]
    if provider == "anthropic":
        payload = {"type": "error", "error": {"type": "permission_error", "message": message}}
    elif provider == "gemini":
        payload = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": message}}
    elif provider == "mcp":
        code = _MCP_ERROR_REVIEW if review else _MCP_ERROR_DENY
        error: dict[str, Any] = {"code": code, "message": message}
        if review_id:
            # JSON-RPC clients read the error object and never see headers, so
            # for MCP the ticket has to ride in `data` or it is unreachable.
            error["data"] = {"review_id": review_id, "retry_header": REVIEW_HEADER}
        payload = {"jsonrpc": "2.0", "error": error, "id": None}
    else:
        payload = {
            "error": {
                "type": "permission_error",
                "code": "governance_review_required" if review else "governance_denied",
                "message": message,
                **({"review_id": review_id} if review_id else {}),
            }
        }
    return JSONResponse(payload, status_code=403, headers=headers)


def _record_observation(
    store: deque[dict[str, Any]],
    *,
    path: str,
    snapshot: Any,
    decision: Decision,
    agent_id: str,
    client_name: str,
    client_version: str | None,
) -> None:
    """Append to the observations ring buffer.

    No message content, no credentials -- only routing/decision metadata a
    conformance probe needs to prove its traffic reached the gateway. client_name/
    client_version come only from user-agent and x-stainless-* headers -- see
    parapetai_gateway.fingerprint -- never from the body.
    """
    store.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": snapshot.provider,
            "action": snapshot.action,
            "path": path,
            "model": snapshot.model,
            "tool_name": snapshot.tool_name,
            "decision": decision.effect,
            "agent_id": agent_id,
            "client_name": client_name,
            "client_version": client_version,
        }
    )


def _audit(decision: Decision, caller: Caller, snapshot: Any, context: dict[str, Any]) -> None:
    # governance_runtime.audit() is the SAME "decision" event this function
    # used to build by hand -- it strips content-bearing keys itself
    # (content_free(), a strict superset of the messages_preview-only strip
    # this used to do) and, when configure_otel() has run (server/main.py,
    # only when a control plane is configured), ALSO ships it as a real OTel
    # LogRecord to the control plane's /v1/logs -- a no-op otherwise, so this
    # call is unconditionally safe with no control plane configured too.
    governance_runtime.audit(
        decision,
        principal=caller.principal,
        snapshot=snapshot,
        resource=f'Resource::"{snapshot.provider}"',
        context=context,
    )
    if decision.evaluation_ms > settings.decision_budget_ms:
        log.warning(
            "slow_decision", ms=round(decision.evaluation_ms, 2), budget=settings.decision_budget_ms
        )


def _log_prompt_content(caller: Caller, snapshot: Any, decision: Decision) -> None:
    """Opt-in only (PARAPETAI_LOG_PROMPTS) -- prompt content is sensitive (PII,
    secrets, proprietary data). A distinct "prompt_content" event, never
    folded into "decision": that keeps the routing/decision audit trail
    content-free regardless of whether this is enabled, and lets an operator
    filter, redirect, or drop this specific event stream independently (e.g.
    route it away from a shared log aggregator entirely). See docs/adr/0005.

    Deliberately narrow: message text only, for Cedar policy analysis. Never
    reads Authorization/x-api-key/x-goog-api-key -- those never touch
    Snapshot in the first place, so there is nothing here to leak.
    """
    if not snapshot.messages_preview:
        return
    log.info(
        "prompt_content",
        principal=caller.principal,
        agent_id=caller.agent_id,
        provider=snapshot.provider,
        model=snapshot.model,
        action=snapshot.action,
        decision=decision.effect,
        prompt=snapshot.messages_preview,
    )
