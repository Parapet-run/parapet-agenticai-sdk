"""GET /__parapetai/observations -- the conformance harness's only way to prove a
framework's traffic actually reached the gateway. Identity is a /a/{agent_id}
path-prefix claim, not a token -- see docs/adr/0003."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"


@pytest.fixture
def client() -> TestClient:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    app = create_app(engine)
    return TestClient(app)


def _call_tool(
    client: TestClient, tool_name: str, agent_id: str | None = None, **headers: str
) -> None:
    path = f"/a/{agent_id}/mcp" if agent_id is not None else "/mcp"
    client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {"secret": "sh"}},
        },
        headers=headers,
    )


def test_observation_recorded_with_agent_id(client: TestClient) -> None:
    # execute_shell is denied by policy -- no upstream forward, so this needs
    # no network mocking to reach the observation-recording line.
    _call_tool(client, "execute_shell", agent_id="conformance-mcp")

    resp = client.get("/__parapetai/observations", params={"agent_id": "conformance-mcp"})
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert len(records) == 1
    record = records[0]
    assert record["provider"] == "mcp"
    assert record["action"] == "tool_call"
    assert record["tool_name"] == "execute_shell"
    assert record["decision"] == "deny"
    assert record["agent_id"] == "conformance-mcp"
    assert "timestamp" in record
    # TestClient's own default UA -- unrecognised, recorded raw, not dropped.
    assert record["client_name"] == "unknown"
    assert record["client_version"] == "testclient"


def test_observation_records_recognised_client_fingerprint(client: TestClient) -> None:
    _call_tool(
        client,
        "execute_shell",
        agent_id="fingerprint-check",
        **{"user-agent": "OpenAI/Python 2.52.0", "x-stainless-package-version": "2.52.0"},
    )

    resp = client.get("/__parapetai/observations", params={"agent_id": "fingerprint-check"})
    record = resp.json()["records"][0]
    assert record["client_name"] == "openai-python"
    assert record["client_version"] == "2.52.0"


def test_observation_filters_by_agent_id(client: TestClient) -> None:
    _call_tool(client, "execute_shell", agent_id="agent-a")
    _call_tool(client, "delete_file", agent_id="agent-b")

    resp = client.get("/__parapetai/observations", params={"agent_id": "agent-b"})
    records = resp.json()["records"]
    assert len(records) == 1
    assert records[0]["tool_name"] == "delete_file"


def test_no_prefix_resolves_to_anonymous_and_is_still_evaluated(client: TestClient) -> None:
    _call_tool(client, "execute_shell")  # no /a/ prefix at all

    resp = client.get("/__parapetai/observations", params={"agent_id": "anonymous"})
    records = resp.json()["records"]
    assert len(records) == 1
    assert records[0]["tool_name"] == "execute_shell"
    assert records[0]["decision"] == "deny"  # anonymous is a real principal, not a bypass


def test_empty_agent_id_segment_falls_through_to_anonymous(client: TestClient) -> None:
    client.post(
        "/a//mcp",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "execute_shell", "arguments": {}},
        },
    )

    resp = client.get("/__parapetai/observations", params={"agent_id": "anonymous"})
    assert len(resp.json()["records"]) == 1


def test_observation_omits_message_content_and_credentials(client: TestClient) -> None:
    _call_tool(client, "execute_shell", agent_id="no-leak", Authorization="Bearer super-secret")

    resp = client.get("/__parapetai/observations", params={"agent_id": "no-leak"})
    record = resp.json()["records"][0]
    assert "tool_args" not in record
    assert "arguments" not in record
    dumped = str(record)
    assert "secret" not in dumped  # neither the tool_args value nor "super-secret"


def test_observations_readable_without_prefix(client: TestClient) -> None:
    resp = client.get("/__parapetai/observations")
    assert resp.status_code == 200
    assert resp.json() == {"records": []}


def test_observations_ring_buffer_caps_at_500(client: TestClient) -> None:
    for i in range(520):
        _call_tool(client, "execute_shell", agent_id=f"a{i}")

    resp = client.get("/__parapetai/observations")
    assert len(resp.json()["records"]) == 500
