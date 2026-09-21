"""The health listener and the admin-route switch.

Both exist for one deployment shape: an internet-facing mTLS gateway. A probe
carries no client certificate, so it needs a plain-HTTP port; and that port, and
the agent-facing one, must not leak policy internals.
"""

from __future__ import annotations

import dataclasses
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import parapetai_gateway.server.app as app_module
import pytest
from fastapi.testclient import TestClient
from parapetai_gateway.server.app import create_app
from parapetai_gateway.server.serve import create_health_app, start_health_listener

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"


def _engine() -> PolicyEngine:
    return PolicyEngine(POLICIES, POLICIES / "entities.json")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── the health app ───────────────────────────────────────────────────────


def test_health_and_ready_report_up_and_nothing_else() -> None:
    client = TestClient(create_health_app(_engine()))

    assert client.get("/__parapetai/health").json() == {"status": "ok"}
    ready = client.get("/__parapetai/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}  # no digest, generation or directory


def test_ready_is_503_until_policies_are_loaded() -> None:
    empty = SimpleNamespace(status={"policy_files": 0})

    resp = TestClient(create_health_app(empty)).get("/__parapetai/ready")  # type: ignore[arg-type]

    assert resp.status_code == 503


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/__parapetai/policies"),
        ("POST", "/__parapetai/policies/reload"),
        ("GET", "/__parapetai/observations"),
        ("GET", "/mcp"),
        ("POST", "/mcp"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("GET", "/__parapetai/docs"),
        ("GET", "/"),
    ],
)
def test_the_health_listener_serves_nothing_but_health(method: str, path: str) -> None:
    """If this port is ever reachable by something it should not be, all it can
    learn is whether the gateway is up -- and it can drive no proxying at all."""
    resp = TestClient(create_health_app(_engine())).request(method, path)

    assert resp.status_code in (404, 405)


def test_a_real_health_listener_answers_plain_http_on_its_own_port() -> None:
    port = _free_port()
    server, _ = start_health_listener(_engine(), host="127.0.0.1", port=port)
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "health listener did not start"
            time.sleep(0.02)

        resp = httpx.get(f"http://127.0.0.1:{port}/__parapetai/ready", timeout=5)

        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}
    finally:
        server.should_exit = True


# ── the admin-route switch on the agent-facing app ───────────────────────


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> TestClient:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, **overrides)
    )
    return TestClient(create_app(_engine()))


_ADMIN = [
    ("GET", "/__parapetai/policies"),
    ("POST", "/__parapetai/policies/reload"),
    ("GET", "/__parapetai/observations"),
]


@pytest.mark.parametrize(("method", "path"), _ADMIN)
def test_admin_routes_are_on_by_default_so_existing_deployments_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    resp = _client(monkeypatch).request(method, path)

    assert resp.status_code == 200


@pytest.mark.parametrize(("method", "path"), _ADMIN)
def test_admin_routes_can_be_switched_off_and_do_not_fall_through_to_the_proxy(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    resp = _client(monkeypatch, admin_routes=False).request(method, path)

    # 404, not a proxied/parsed/evaluated request (which would be a 502 or a
    # policy decision), and not the data.
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


def test_with_admin_routes_off_ready_no_longer_reveals_the_policy_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _client(monkeypatch).get("/__parapetai/ready").json()
    off = _client(monkeypatch, admin_routes=False).get("/__parapetai/ready").json()

    assert "digest" in on and "policy_dir" in on
    assert off == {"status": "ready"}


def test_health_stays_available_with_admin_routes_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client(monkeypatch, admin_routes=False).get("/__parapetai/health").status_code == 200


@pytest.mark.parametrize("admin", [True, False])
def test_an_unknown_path_in_the_gateways_namespace_is_never_proxied(
    monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    resp = _client(monkeypatch, admin_routes=admin).get("/__parapetai/anything-else")

    assert resp.status_code == 404
