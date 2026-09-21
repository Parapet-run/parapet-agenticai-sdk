"""Building the listeners. Kept out of main.py so the tests drive exactly the
wiring production uses, rather than a copy that can drift."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from parapetai_agent.policy.engine import PolicyEngine
from parapetai_gateway.identity.mtls import uvicorn_tls_kwargs
from parapetai_gateway.tls_reload import TlsFiles, TlsReloader

log = structlog.get_logger(__name__)


def tls_files_from_settings(settings: Any) -> TlsFiles | None:
    """The mTLS material the settings name, or None when mTLS is off."""
    if not settings.tls_client_ca:
        return None
    if not settings.tls_cert:
        # uvicorn_tls_kwargs raises the operator-facing message; fail the same way.
        uvicorn_tls_kwargs(cert=None, key=None, client_ca="", client_auth="required")
    return TlsFiles(
        cert=Path(settings.tls_cert),
        key=Path(settings.tls_key) if settings.tls_key else None,
        client_ca=Path(settings.tls_client_ca),
        client_auth=settings.tls_client_auth,
    )


def build_server(
    app: Any,
    *,
    host: str,
    port: int,
    tls: TlsFiles | None = None,
    tls_reload_interval_s: float = 30.0,
    on_tls_event: Callable[[str, dict[str, Any]], None] | None = None,
    **uvicorn_kwargs: Any,
) -> tuple[uvicorn.Server, TlsReloader | None]:
    """The agent-facing listener. With `tls`, it terminates mTLS itself and the
    returned reloader hot-swaps rotated certificates (start it with
    `reloader.start()`); unusable material raises here, at startup."""
    reloader: TlsReloader | None = None
    tls_kwargs: dict[str, Any] = {}
    if tls is not None:
        # The reloader fingerprints the files BEFORE uvicorn reads them below,
        # so a rotation landing in between is still picked up on the next poll.
        reloader = TlsReloader(tls, interval_s=tls_reload_interval_s, on_event=on_tls_event)
        tls_kwargs = uvicorn_tls_kwargs(
            cert=str(tls.cert),
            key=str(tls.key) if tls.key else None,
            client_ca=str(tls.client_ca),
            client_auth=tls.client_auth,
        )
    config = uvicorn.Config(app, host=host, port=port, **tls_kwargs, **uvicorn_kwargs)
    config.load()  # builds config.ssl now, so a bad certificate fails here
    if reloader is not None:
        assert config.ssl is not None
        reloader.attach(config.ssl)
    return uvicorn.Server(config), reloader


def create_health_app(engine: PolicyEngine) -> FastAPI:
    """The whole surface of the health listener: two routes, up/down only.

    No policy digest, no generation, no paths, no catch-all proxy route -- so
    even if this port were reachable by something it should not be, all it can
    learn is whether the gateway is up. Everything else 404s.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/__parapetai/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/__parapetai/ready")
    async def ready() -> Response:
        if engine.status["policy_files"] == 0:
            return JSONResponse({"status": "no policies"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app


def start_health_listener(
    engine: PolicyEngine, *, host: str, port: int
) -> tuple[uvicorn.Server, threading.Thread]:
    """Runs the health app on its own port in a daemon thread. Plain HTTP by
    design: an orchestrator's probe carries no client certificate."""
    server = uvicorn.Server(
        uvicorn.Config(create_health_app(engine), host=host, port=port, log_level="warning")
    )

    def _run() -> None:
        try:
            server.run()
        except Exception:  # a dead health port must be loud, not silent
            log.exception("health_listener_failed", port=port)

    thread = threading.Thread(target=_run, name="health-listener", daemon=True)
    thread.start()
    return server, thread
