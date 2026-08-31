"""Gateway entrypoint. Starts the policy watcher (and, if a control plane is
configured, the bundle poller -- which also sends this PEP's fleet
heartbeat once per poll cycle, see parapetai_agent.control_plane.run_bundle_poller)
then serves."""

from __future__ import annotations

import argparse
import threading
from importlib.metadata import PackageNotFoundError, version

import structlog
import uvicorn
from watchfiles import watch

from parapetai_agent import pep_identity
from parapetai_agent.control_plane import (
    ReviewClient,
    ensure_pep_identity,
    poll_once,
    run_bundle_poller,
)
from parapetai_agent.governance_runtime import configure_otel
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_gateway.config import settings
from parapetai_gateway.server.app import create_app

log = structlog.get_logger(__name__)


def _installed_version() -> str:
    try:
        return version("parapetai-gateway")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _watch(engine: PolicyEngine) -> None:
    # force_polling: ConfigMap volumes relink `..data` atomically and inotify on
    # projected volumes is unreliable. Watch the directory, poll for the swap.
    for _ in watch(settings.policy_dir, force_polling=True, poll_delay_ms=2000):
        result = engine.reload()
        if result["status"] == "failed":
            log.error("reload_rejected_serving_previous", **result)


def _parse_args() -> argparse.Namespace:
    # Every flag falls back to its PARAPETAI_* env var (parapetai_gateway.config.Settings)
    # when omitted -- "command line or env file", so a containerised
    # deployment can use either without code changes.
    parser = argparse.ArgumentParser(description="Parapet PEP")
    parser.add_argument("--agent-id", default=settings.agent_id)
    parser.add_argument("--agent-secret", default=settings.agent_secret)
    parser.add_argument("--control-plane-url", default=settings.control_plane_url)
    parser.add_argument("--otlp-endpoint", default=settings.otlp_endpoint)
    parser.add_argument("--pep-id", default=settings.pep_id)
    return parser.parse_args()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    args = _parse_args()

    # Opt-in: only when both an agent_id-owning secret and a control plane
    # URL are configured. With neither, the gateway behaves exactly as
    # before -- local policy_dir only, no outbound calls.
    control_plane_configured = bool(args.control_plane_url and args.agent_secret)

    if control_plane_configured:
        # Ships every _audit()-recorded decision (server/app.py) to the
        # control plane as a real OTel LogRecord (docs/OBSERVABILITY.md) --
        # console=False since structlog's own "decision" JSON line already
        # covers local visibility (az containerapp logs / stdout); this adds
        # the control-plane-visible copy, not a replacement for it.
        # otlp_endpoint falls back to control_plane_url itself, same
        # resolution order as parapetai_agent.maf.build_middleware's
        # identical fallback -- a dedicated PARAPETAI_OTLP_ENDPOINT is only
        # needed when the OTLP receiver lives somewhere else.
        configure_otel(
            service_name="parapetai-gateway",
            otlp_endpoint=settings.otlp_endpoint or args.control_plane_url,
            otlp_headers={"Authorization": f"Bearer {args.agent_secret}"},
            console=False,
            # "buffered" (configure_otel's own default) holds up to 2 minutes
            # before flushing -- right for a high-throughput embedded agent,
            # wrong here: a standalone gateway's call volume is typically low
            # enough that per-decision export overhead doesn't matter, and a
            # 2-minute delay makes "did this reach the control plane?" look
            # broken during exactly the kind of interactive testing this
            # deployment is for.
            log_mode="streaming",
        )

    private_key = None
    key_path = pep_identity.default_key_path()
    if control_plane_configured:
        # Ed25519 identity, loaded/created and registered BEFORE the first
        # poll_once() below -- see parapetai_agent.control_plane.ensure_pep_identity's
        # docstring. Best-effort like everything else in this control-plane
        # setup sequence: registration failing doesn't block startup, it
        # just means the control plane won't enforce signatures for this
        # agent yet (parapetai_control/keys.py's gradual-enforcement design).
        private_key = ensure_pep_identity(args.control_plane_url, args.agent_secret, key_path)

        # Synchronous, BEFORE constructing PolicyEngine: its __init__ calls
        # reload() immediately and raises if policy_dir has no .cedar files
        # yet (PolicyLoadError) -- on a cold start with an empty policy_dir,
        # the background poller below would never get a chance to run and
        # populate it. A failed fetch here is not fatal on its own: if
        # policy_dir already has files from a previous run, PolicyEngine
        # still loads those (poll_once leaves disk untouched on failure);
        # if it doesn't, PolicyEngine raises below exactly as it always
        # has -- fail closed, never fail open.
        poll_once(
            args.control_plane_url,
            args.agent_secret,
            settings.policy_dir,
            None,
            private_key=private_key,
        )

    engine = PolicyEngine(settings.policy_dir, settings.entities_path)
    threading.Thread(target=_watch, args=(engine,), daemon=True, name="policy-watch").start()

    if control_plane_configured:
        threading.Thread(
            target=run_bundle_poller,
            args=(args.control_plane_url, args.agent_secret, settings.policy_dir),
            kwargs={
                "interval_s": settings.bundle_poll_interval_s,
                "engine": engine,
                "pep_id": args.pep_id,
                "version": _installed_version(),
                "mode": settings.mode,
                "private_key": private_key,
                "key_path": key_path,
            },
            daemon=True,
            name="bundle-poll",
        ).start()
        log.info(
            "bundle_poller_started",
            agent_id=args.agent_id,
            pep_id=args.pep_id,
            control_plane_url=args.control_plane_url,
            interval_s=settings.bundle_poll_interval_s,
        )

    # A held call can only be escalated where there is a queue to escalate to.
    # None without a control plane, which keeps the no-control-plane gateway
    # behaving exactly as it did before approvals existed.
    reviews = (
        ReviewClient(
            control_plane_url=args.control_plane_url,
            agent_secret=args.agent_secret,
            agent_id=args.agent_id,
            private_key=private_key,
            pep_id=args.pep_id,
        )
        if control_plane_configured
        else None
    )

    log.info("gateway_starting", mode=settings.mode, port=settings.port, **engine.status)
    uvicorn.run(
        create_app(engine, reviews),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        # Behind a TLS-terminating ingress (Azure Container Apps, any L7 LB),
        # the real client sees https but this process only ever accepts
        # plain HTTP on its container port. Without this, request.base_url
        # reports http://, which lands in the OAuth issuer/endpoint URLs
        # returned by mcp_oauth's metadata routes -- a client that connects
        # via https then gets told its own authorization_endpoint is http.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
