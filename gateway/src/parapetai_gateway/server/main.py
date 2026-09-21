"""Gateway entrypoint. Starts the policy watcher (and, if a control plane is
configured, the bundle poller -- which also sends this PEP's fleet
heartbeat once per poll cycle, see parapetai_agent.control_plane.run_bundle_poller)
then serves."""

from __future__ import annotations

import argparse
import inspect
import threading
from importlib.metadata import PackageNotFoundError, version

import structlog
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
from parapetai_gateway.server.serve import (
    build_server,
    start_health_listener,
    tls_files_from_settings,
)
from parapetai_gateway.status import GatewayStatus

log = structlog.get_logger(__name__)


def _installed_version() -> str:
    try:
        return version("parapetai-gateway")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _details_kwargs(status: GatewayStatus) -> dict[str, object]:
    """`details_provider` for the heartbeat, IF the installed parapetai-agent has it.

    The gateway image installs parapetai-agent from a package index, which can be
    older than this gateway. Passing an argument the poller does not accept would
    raise in the poller thread and silently stop policy refresh and heartbeats,
    a far worse outcome than a gateway that merely reports less. So detect it,
    say so loudly, and carry on without the status block.
    """
    if "details_provider" in inspect.signature(run_bundle_poller).parameters:
        return {"details_provider": status.snapshot}
    log.warning(
        "heartbeat_details_unsupported_by_installed_sdk",
        hint="upgrade parapetai-agent so the control plane can show this gateway's "
        "certificates and connected agents; the gateway otherwise works normally",
    )
    return {}


def _identity_methods(app: object) -> list[str]:
    resolver = getattr(getattr(app, "state", None), "identity", None)
    if resolver is None:
        return []
    return sorted(
        m for m, on in (("mtls", resolver.mtls_enabled), ("jwt", resolver.jwt_enabled)) if on
    )


def _binding_count(app: object) -> int | None:
    resolver = getattr(getattr(app, "state", None), "identity", None)
    return len(resolver.bindings) if resolver is not None else None


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
        # covers local visibility (the platform's container logs / stdout); this adds
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

    # Built before the bundle-poll thread starts (moved ahead of it,
    # unlike before Phase B): app.state.vsp_budget must exist so its
    # update_from_bundle_meta can be wired as the poller's on_bundle_meta
    # callback below -- auth-integrations.md §10.16 Phase B.
    # What this gateway tells the control plane about itself, beyond the plain
    # heartbeat: certificate state, rotations, and which identities connect.
    status = GatewayStatus(site=settings.site, window_s=settings.connected_window_s)
    app = create_app(engine, reviews, status=status)

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
                # auth-integrations.md §10.3/§10.17: every poll cycle's
                # full bundle response feeds the same server-controlled
                # saturation signal the in-process SDK already honors
                # (observation.CollectionBudget.update_from_bundle_meta),
                # so a gateway-fronted MCP fleet gets the identical
                # collect-until-N-then-stop behavior, not a separate one.
                "on_bundle_meta": app.state.vsp_budget.update_from_bundle_meta,
                **_details_kwargs(status),
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

    # mTLS is terminated here, in uvicorn. Built BEFORE serving so unusable
    # certificate material raises now instead of running without client
    # verification.
    tls = tls_files_from_settings(settings)
    if tls is not None and settings.health_port is None:
        log.warning(
            "mtls_without_health_port",
            hint="set PARAPETAI_HEALTH_PORT: plain-HTTP probes cannot reach a TLS port",
        )

    server, reloader = build_server(
        app,
        on_tls_event=status.on_tls_event,
        host=settings.host,
        port=settings.port,
        tls=tls,
        tls_reload_interval_s=settings.tls_reload_interval_s,
        log_level=settings.log_level,
        # Behind a TLS-terminating ingress (a managed container platform, any L7 LB),
        # the real client sees https but this process only ever accepts
        # plain HTTP on its container port. Without this, request.base_url
        # reports http://, which lands in the OAuth issuer/endpoint URLs
        # returned by mcp_oauth's metadata routes -- a client that connects
        # via https then gets told its own authorization_endpoint is http.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    if tls is not None:
        status.watch_tls(tls.cert, tls.key, tls.client_ca)
    status.describe_config(
        mtls=tls is not None,
        client_auth=tls.client_auth if tls else None,
        tls_reload_interval_s=settings.tls_reload_interval_s if tls else None,
        identity_required=settings.require_verified_identity,
        identity_methods=_identity_methods(app),
        bindings=_binding_count(app),
        admin_routes=settings.admin_routes,
        health_port=settings.health_port,
        mode=settings.mode,
    )
    status.add_event("gateway_started", version=_installed_version())
    if reloader is not None:
        reloader.start()  # no-op when PARAPETAI_TLS_RELOAD_INTERVAL_S=0
    if settings.health_port is not None:
        start_health_listener(engine, host=settings.host, port=settings.health_port)

    log.info(
        "gateway_starting",
        mode=settings.mode,
        port=settings.port,
        health_port=settings.health_port,
        mtls=tls is not None,
        tls_reload=bool(reloader and settings.tls_reload_interval_s > 0),
        admin_routes=settings.admin_routes,
        identity_required=settings.require_verified_identity,
        **engine.status,
    )
    server.run()


if __name__ == "__main__":
    main()
