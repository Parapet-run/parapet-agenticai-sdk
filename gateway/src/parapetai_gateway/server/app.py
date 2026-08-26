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

import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from parapetai_agent.identity import Caller, resolve_from_path
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.providers.parsers import parse_request

from parapetai_gateway.config import settings
from parapetai_gateway.fingerprint import fingerprint

log = structlog.get_logger(__name__)
router = APIRouter()

# Dev/test surface for the conformance harness: proves a framework's traffic
# actually reached the gateway. Deliberately excludes message content and
# credentials -- see _record_observation.
_OBSERVATIONS_CAP = 500


def create_app(engine: PolicyEngine) -> FastAPI:
    app = FastAPI(title="Parapet", docs_url="/__parapetai/docs", redoc_url=None)
    app.state.engine = engine
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

    app.include_router(router)
    return app


def create_app_factory() -> FastAPI:
    """Zero-arg factory for `uvicorn --factory` (the `make dev` hot-reload loop).

    uvicorn's reloader re-imports this module in a fresh subprocess and calls
    the factory with no arguments, so the engine must be built from
    env-driven settings here -- same construction as the production
    entrypoint in server/main.py, minus the policy-file watcher thread.
    """
    engine = PolicyEngine(settings.policy_dir, settings.entities_path)
    return create_app(engine)


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
    context = {
        **snapshot.to_context(),
        "method": request.method,
        "path": path,
        "tenant": caller.tenant,
        "trust_tier": caller.trust_tier,
    }

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
    # neither may reach the upstream. A review differs only in what the caller
    # is told (see _provider_shaped_block) -- the gateway itself has no
    # approval workflow, so a held call is refused now and the agent retries
    # once a human has approved it out of band.
    if not decision.allowed:
        if settings.enforcing:
            return _provider_shaped_block(snapshot.provider, decision)
        log.warning(
            "monitor_would_block",
            agent=caller.agent_id,
            action=snapshot.action,
            provider=snapshot.provider,
            effect=decision.effect,
            reason=decision.reason,
        )

    upstream = settings.upstream_for(snapshot.provider)
    if upstream is None:
        return _error(502, "no_upstream", f"no upstream configured for {snapshot.provider}")

    return await _forward(client, upstream, path, request, raw, snapshot.stream)


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

    async def relay() -> AsyncIterator[bytes]:
        # Chunks are relayed untouched and in order. Do not accumulate: SSE
        # consumers time out, and reassembly here would add latency to every
        # token.
        resp = await client.send(req, stream=True)
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream")


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


def _provider_shaped_block(provider: str, decision: Decision) -> JSONResponse:
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
    headers = {
        "x-parapetai-decision": decision.effect,
        "x-parapetai-policy-generation": str(decision.policy_generation),
    }

    payload: dict[str, Any]
    if provider == "anthropic":
        payload = {"type": "error", "error": {"type": "permission_error", "message": message}}
    elif provider == "gemini":
        payload = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": message}}
    elif provider == "mcp":
        code = _MCP_ERROR_REVIEW if review else _MCP_ERROR_DENY
        payload = {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": None}
    else:
        payload = {
            "error": {
                "type": "permission_error",
                "code": "governance_review_required" if review else "governance_denied",
                "message": message,
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
    record = decision.to_audit_record(
        principal=caller.principal,
        action=snapshot.action,
        resource=f'Resource::"{snapshot.provider}"',
        context={k: v for k, v in context.items() if k != "messages_preview"},
    )
    log.info("decision", **record)
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
