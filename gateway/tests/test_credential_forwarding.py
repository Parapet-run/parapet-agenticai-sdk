"""Credential forwarding is mode-gated (docs/adr/0003): passthrough (default)
forwards the caller's own Authorization/x-api-key header to upstream
unchanged; broker (opt-in, for later) strips it and injects the gateway-held
PARAPETAI_<PROVIDER>_KEY instead."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import parapetai_gateway.server.app as app_module
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"


@pytest.fixture
def client() -> TestClient:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine))


@respx.mock
def test_passthrough_forwards_callers_credential_unchanged(client: TestClient) -> None:
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(200, json={"ok": True})
    )

    client.post(
        "/a/probe/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer caller-owned-key"},
    )

    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer caller-owned-key"


@respx.mock
def test_broker_strips_callers_credential_and_injects_gateway_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, credential_mode="broker")
    )
    monkeypatch.setenv("PARAPETAI_OPENAI_KEY", "gateway-held-key")
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(200, json={"ok": True})
    )

    client.post(
        "/a/probe/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer caller-owned-key"},
    )

    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer gateway-held-key"
