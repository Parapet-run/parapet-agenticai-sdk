"""The gateway must decide, audit and trace a call the way the in-process
adapters do.

server/app.py used to call `PolicyEngine.evaluate` directly. That path had no
decision span, so the audit LogRecord carried no trace/span id; passed no
`stage`, so `@stage("post")` policies applied to a request-side decision;
never tagged `framework`; and could not use `vendor_scoped_resources`. It now
drives the same `GovernanceHook` as `Governor`/`GovernedAgent`/`GovernedRunner`,
and these tests pin that so the two cannot drift apart again.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Any

import parapetai_gateway.server.app as app_module
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace
from parapetai_gateway.config import Settings
from parapetai_gateway.server.app import create_app

from parapetai_agent import Governor
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook

from .conftest import SPAN_EXPORTER

POLICIES = Path(__file__).resolve().parents[2] / "policies"

# execute_shell is denied by policies/20-tools.cedar, so no upstream is needed
# to reach the code under test.
_DENIED_TOOL = "execute_shell"


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> TestClient:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, **overrides)
    )
    return TestClient(create_app(PolicyEngine(POLICIES, POLICIES / "entities.json")))


def _tool_call(client: TestClient, rpc_id: Any = 1, agent: str = "acme", tool: str = _DENIED_TOOL):
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"cmd": "ls"}},
    }
    if rpc_id is not None:
        body["id"] = rpc_id
    return client.post(f"/a/{agent}/mcp", json=body)


@pytest.fixture(autouse=True)
def _clear_spans() -> None:
    SPAN_EXPORTER.clear()


def test_decision_span_carries_the_same_attributes_as_an_in_process_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tool_call(_client(monkeypatch))

    (span,) = [s for s in SPAN_EXPORTER.get_finished_spans() if s.name == "parapetai.tool_call"]
    attrs = dict(span.attributes or {})
    assert attrs["principal"] == 'Agent::"acme"'
    assert attrs["action"] == "tool_call"
    assert attrs["resource"] == 'Resource::"mcp"'
    assert attrs["stage"] == "pre"
    assert attrs["decision"] == "deny"
    assert attrs["tool_name"] == _DENIED_TOOL
    assert attrs["framework"] == "gateway"
    assert attrs["openinference.span.kind"] == "TOOL"
    assert span.status.status_code == otel_trace.StatusCode.ERROR


def test_gateway_and_governor_agree_on_a_decision_and_its_span_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governor = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")
    # Governor leaves its decision span open until the trace block exits.
    with governor.trace():
        in_process = governor.authorize_tool(_DENIED_TOOL, {"cmd": "ls"}, raise_on_deny=False)
    governor_spans = [
        s for s in SPAN_EXPORTER.get_finished_spans() if s.name == "parapetai.tool_call"
    ]
    SPAN_EXPORTER.clear()

    resp = _tool_call(_client(monkeypatch))
    (gateway_span,) = [
        s for s in SPAN_EXPORTER.get_finished_spans() if s.name == "parapetai.tool_call"
    ]

    assert resp.headers["x-parapetai-decision"] == in_process.effect
    assert gateway_span.attributes["decision"] == in_process.effect
    # Same attribute vocabulary: everything Governor's decision span carries
    # about the decision itself, the gateway's does too. Resource and
    # framework legitimately differ (provider "mcp" vs "govern").
    shared = {"principal", "action", "stage", "decision", "tool_name", "tool_args"}
    assert governor_spans, "Governor opened no decision span -- the comparison is vacuous"
    for key in shared:
        assert key in (governor_spans[0].attributes or {}), key
        assert key in (gateway_span.attributes or {}), key


def test_audit_runs_inside_the_decision_span_so_the_log_correlates(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _tool_call(_client(monkeypatch))

    (record,) = audited
    assert record["span_active"] is True


def test_context_carries_the_framework_tag_and_gateway_keys(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _tool_call(_client(monkeypatch))

    context = audited[0]["context"]
    assert context["framework"] == "gateway"
    assert context["method"] == "POST"
    assert context["path"] == "/mcp"
    assert context["tenant"] == "default"


def test_audited_principal_and_resource_are_what_cedar_evaluated(
    monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _tool_call(_client(monkeypatch), agent="fib-sales")

    assert audited[0]["principal"] == 'Agent::"fib-sales"'
    assert audited[0]["resource"] == 'Resource::"mcp"'


@pytest.mark.parametrize(
    ("scoped", "expected"), [(False, 'Resource::"mcp"'), (True, 'Resource::"undeclared"')]
)
def test_vendor_scoped_resources_is_reachable_from_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
    audited: list[dict[str, Any]],
    scoped: bool,
    expected: str,
) -> None:
    # No PARAPETAI_MCP_TOOL_MAP is set here, so a tool call with the flag on must
    # land on the fail-closed `undeclared` resource, never silently fall back
    # to the provider-scoped one (hooks.py finding #11).
    _tool_call(_client(monkeypatch, vendor_scoped_resources=scoped))

    assert audited[0]["resource"] == expected


def test_every_governance_hook_flag_has_a_gateway_setting() -> None:
    """The gateway is an integration surface that builds a GovernanceHook, so
    it must expose every opt-in flag the in-process surfaces do (the same rule
    tests/test_governance_surface_parity.py enforces for them)."""
    params = inspect.signature(GovernanceHook.__init__).parameters
    flags = {n for n in params if n not in {"self", "engine", "caller", "on_decision"}}
    assert flags, "GovernanceHook grew no flags at all -- this test would be vacuous"
    missing = flags - {f.name for f in dataclasses.fields(Settings)}
    assert not missing, (
        f"gateway Settings has no field for GovernanceHook flag(s) {sorted(missing)}"
    )


@pytest.mark.parametrize(
    ("rpc_id", "expected"),
    [(7, 7), ("call-1", "call-1"), (0, 0), (None, None)],
)
def test_mcp_block_echoes_the_request_id(
    monkeypatch: pytest.MonkeyPatch, rpc_id: Any, expected: Any
) -> None:
    resp = _tool_call(_client(monkeypatch), rpc_id=rpc_id)

    assert resp.status_code == 403
    assert resp.json()["id"] == expected


@pytest.mark.parametrize("bad_id", [True, {"x": 1}, [1], 1.5])
def test_mcp_block_never_reflects_a_non_spec_id(
    monkeypatch: pytest.MonkeyPatch, bad_id: Any
) -> None:
    resp = _tool_call(_client(monkeypatch), rpc_id=bad_id)

    assert resp.status_code == 403
    assert resp.json()["id"] is None


_BATCH = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": _DENIED_TOOL}}]


def test_a_batch_containing_a_denied_tool_call_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a batch body is a JSON array, which MCPParser leaves
    unparsed, so it used to be evaluated as the coarse `http_request` action
    and forwarded -- the tools/call inside was never checked."""
    resp = _client(monkeypatch).post("/a/acme/mcp", json=_BATCH)

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600
    assert resp.json()["id"] is None
    assert resp.headers["x-parapetai-decision"] == "deny"


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"{not json", "application/json"),
        (
            b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"execute_shell"}}',
            "text/plain",
        ),
        (b'"a bare string"', "application/json"),
    ],
)
def test_an_unparseable_mcp_body_is_refused(
    monkeypatch: pytest.MonkeyPatch, content: bytes, content_type: str
) -> None:
    resp = _client(monkeypatch).post(
        "/a/acme/mcp", content=content, headers={"content-type": content_type}
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_bodyless_mcp_requests_are_not_refused_as_unparseable(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    # The stream-open GET and session-ending DELETE carry no body. They must
    # still reach the normal path (here: no upstream configured -> 502), not
    # be swallowed by the unparsed-body guard.
    resp = _client(monkeypatch).request(method, "/a/acme/mcp")

    assert resp.status_code != 400


def test_a_well_formed_single_call_still_reaches_policy_not_the_parse_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _tool_call(_client(monkeypatch))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == -32000  # denied by Cedar, not "invalid request"


def test_monitor_mode_logs_but_does_not_block_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _client(monkeypatch, mode="monitor").post("/a/acme/mcp", json=_BATCH)

    # Monitor mode never blocks: it proceeds (no upstream configured -> 502).
    assert resp.status_code == 502
