"""Vendor tool mapping: an MCP tool name -> what it does against which vendor.

Unit tests pin the mapping rules and, above all, that a malformed map stops
startup rather than leaving tools silently unclassified. The end-to-end tests
use a real Cedar policy to prove the point of the feature: Salesforce reads and
Salesforce deletes become distinguishable, through the gateway.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import parapetai_gateway.server.app as app_module
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from parapetai_gateway.server.app import create_app
from parapetai_gateway.vendor_map import ToolMap, ToolMapError, build_tool_map

from parapetai_agent.policy.engine import PolicyEngine

_SF_DELETE = {"vendor_system": "salesforce", "resource_type": "Case", "crud_action": "delete"}
_SF_GENERIC = {
    "vendor_system": "salesforce",
    "resource_type": "Record",
    "crud_action_from": {
        "arg": "method",
        "map": {"GET": "read", "POST": "create", "PATCH": "update", "DELETE": "delete"},
    },
}


def _map(raw: dict[str, Any]) -> ToolMap:
    return ToolMap.from_dict(raw)


# ── resolution ───────────────────────────────────────────────────────────


def test_a_literal_rule_resolves_with_the_default_operation() -> None:
    facts = _map({"sf": {"delete_case": _SF_DELETE}}).resolve("sf", "delete_case", {})

    assert facts is not None
    assert (facts.vendor_system, facts.vendor_operation, facts.crud_action) == (
        "salesforce",
        "Case.delete",
        "delete",
    )


def test_an_explicit_operation_overrides_the_default() -> None:
    rule = {**_SF_DELETE, "operation": "Case.hard_delete"}

    facts = _map({"sf": {"t": rule}}).resolve("sf", "t", {})

    assert facts is not None
    assert facts.vendor_operation == "Case.hard_delete"


def test_an_unmapped_tool_resolves_to_nothing() -> None:
    assert _map({"sf": {"delete_case": _SF_DELETE}}).resolve("sf", "other_tool", {}) is None


def test_a_named_target_only_matches_its_own_tools() -> None:
    tool_map = _map({"sf": {"delete_case": _SF_DELETE}})

    assert tool_map.resolve("jira", "delete_case", {}) is None
    assert tool_map.resolve(None, "delete_case", {}) is None  # bare /mcp names no target


def test_the_wildcard_target_covers_every_target_including_a_bare_mcp_path() -> None:
    tool_map = _map({"*": {"delete_case": _SF_DELETE}})

    assert tool_map.resolve("anything", "delete_case", {}) is not None
    assert tool_map.resolve(None, "delete_case", {}) is not None


def test_a_named_target_beats_the_wildcard() -> None:
    other = {**_SF_DELETE, "vendor_system": "special"}
    tool_map = _map({"*": {"t": _SF_DELETE}, "sf": {"t": other}})

    named, elsewhere = tool_map.resolve("sf", "t", {}), tool_map.resolve("x", "t", {})

    assert named is not None and named.vendor_system == "special"
    assert elsewhere is not None and elsewhere.vendor_system == "salesforce"


@pytest.mark.parametrize(
    ("method", "verb"),
    [("GET", "read"), ("get", "read"), (" Delete ", "delete"), ("PATCH", "update")],
)
def test_a_generic_tool_derives_its_verb_from_an_argument(method: str, verb: str) -> None:
    facts = _map({"sf": {"sf_request": _SF_GENERIC}}).resolve(
        "sf", "sf_request", {"method": method}
    )

    assert facts is not None
    assert facts.crud_action == verb
    assert facts.vendor_operation == f"Record.{verb}"


@pytest.mark.parametrize(
    "args",
    [{"method": "TRACE"}, {}, {"method": None}, {"method": 5}, {"method": ["GET"]}],
    ids=["unlisted-value", "absent", "null", "number", "list"],
)
def test_an_unrecognised_verb_is_unknown_not_unmapped(args: dict[str, Any]) -> None:
    """The vendor is still known. Dropping the facts would let an unrecognised
    verb slip past a `forbid ... crud_action == "delete"` rule."""
    facts = _map({"sf": {"sf_request": _SF_GENERIC}}).resolve("sf", "sf_request", args)

    assert facts is not None
    assert facts.vendor_system == "salesforce"
    assert facts.crud_action == "unknown"


def test_only_a_top_level_string_argument_can_pick_a_verb() -> None:
    facts = _map({"sf": {"sf_request": _SF_GENERIC}}).resolve(
        "sf", "sf_request", {"nested": {"method": "GET"}}
    )

    assert facts is not None and facts.crud_action == "unknown"


# ── a malformed map stops startup ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param([], id="not-an-object"),
        pytest.param({"sf": []}, id="target-not-an-object"),
        pytest.param({"sf": {"t": "delete"}}, id="rule-not-an-object"),
        pytest.param({"bad target!": {}}, id="bad-target-name"),
        pytest.param({"sf": {"t": {**_SF_DELETE, "crud_actoin": "x"}}}, id="typo-in-a-field-name"),
        pytest.param({"sf": {"t": {"resource_type": "C", "crud_action": "d"}}}, id="no-vendor"),
        pytest.param({"sf": {"t": {"vendor_system": "s", "crud_action": "d"}}}, id="no-resource"),
        pytest.param(
            {"sf": {"t": {"vendor_system": "s", "resource_type": "C"}}}, id="no-verb-at-all"
        ),
        pytest.param(
            {"sf": {"t": {**_SF_DELETE, "crud_action_from": _SF_GENERIC["crud_action_from"]}}},
            id="both-a-literal-and-a-derived-verb",
        ),
        pytest.param({"sf": {"t": {**_SF_DELETE, "crud_action": "Delete"}}}, id="uppercase-verb"),
        pytest.param({"sf": {"t": {**_SF_DELETE, "crud_action": "delete "}}}, id="trailing-space"),
        pytest.param({"sf": {"t": {**_SF_DELETE, "crud_action": "unknown"}}}, id="reserved-verb"),
        pytest.param({"sf": {"t": {**_SF_DELETE, "vendor_system": 'a"b'}}}, id="quote-in-vendor"),
        pytest.param(
            {
                "sf": {
                    "t": {
                        **{k: v for k, v in _SF_GENERIC.items() if k != "crud_action_from"},
                        "crud_action_from": {"arg": "m"},
                    }
                }
            },
            id="derived-verb-without-a-map",
        ),
        pytest.param(
            {
                "sf": {
                    "t": {
                        **{k: v for k, v in _SF_GENERIC.items() if k != "crud_action_from"},
                        "crud_action_from": {"arg": "m", "map": {}},
                    }
                }
            },
            id="derived-verb-with-an-empty-map",
        ),
        pytest.param(
            {
                "sf": {
                    "t": {
                        **{k: v for k, v in _SF_GENERIC.items() if k != "crud_action_from"},
                        "crud_action_from": {"arg": "m", "map": {"GET": "read", "get": "list"}},
                    }
                }
            },
            id="one-value-two-verbs",
        ),
    ],
)
def test_a_malformed_map_is_rejected(raw: Any) -> None:
    with pytest.raises(ToolMapError):
        ToolMap.from_dict(raw)


def test_the_map_loads_from_inline_json_or_a_file(tmp_path: Path) -> None:
    raw = {"sf": {"delete_case": _SF_DELETE}}
    path = tmp_path / "map.json"
    path.write_text(json.dumps(raw))

    from_file, inline = build_tool_map(str(path)), build_tool_map(json.dumps(raw))

    assert from_file is not None and inline is not None
    assert len(from_file) == len(inline) == 1
    assert build_tool_map(None) is None
    assert build_tool_map("") is None


def test_an_unreadable_or_invalid_source_stops_startup(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json")

    with pytest.raises(ToolMapError, match="cannot read"):
        build_tool_map(str(tmp_path / "missing.json"))
    with pytest.raises(ToolMapError, match="not valid JSON"):
        build_tool_map(str(tmp_path / "bad.json"))
    with pytest.raises(ToolMapError, match="not valid JSON"):
        build_tool_map("{oops")


# ── through the gateway, with a real policy ──────────────────────────────

_UPSTREAM = "https://sf.internal/mcp"
_TOOLS = {
    "sf": {
        "get_case": {**_SF_DELETE, "crud_action": "read"},
        "delete_case": _SF_DELETE,
        "sf_request": _SF_GENERIC,
    }
}


def _policies(tmp_path: Path) -> Path:
    """Permit tool calls, then forbid by what they DO rather than what they are
    called: deletes, and anything whose verb could not be determined."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "00-base.cedar").write_text(
        'permit(principal, action == Action::"tool_call", resource);'
    )
    (tmp_path / "10-crud.cedar").write_text(
        '@id("no_vendor_deletes")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has crud_action && context.crud_action == "delete" };\n'
        '@id("no_unknown_verbs")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has crud_action && context.crud_action == "unknown" };'
    )
    return tmp_path


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> TestClient:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(app_module.settings, mcp_upstreams={"sf": _UPSTREAM}, **settings),
    )
    engine = PolicyEngine(_policies(tmp_path))
    return TestClient(create_app(engine, tool_map=ToolMap.from_dict(_TOOLS)))


def _call(client: TestClient, tool: str, **arguments: Any) -> Response:
    return client.post(
        "/a/agent-a/mcp/sf",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )


@respx.mock
def test_cedar_can_tell_a_vendor_read_from_a_vendor_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(_UPSTREAM).mock(return_value=Response(200, json={"jsonrpc": "2.0", "id": 1}))
    client = _client(tmp_path, monkeypatch)

    read = _call(client, "get_case", case_id="500x")
    delete = _call(client, "delete_case", case_id="500x")

    assert read.status_code == 200
    assert delete.status_code == 403
    assert delete.headers["x-parapetai-decision"] == "deny"


@respx.mock
def test_a_generic_tool_is_judged_by_the_verb_in_its_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(_UPSTREAM).mock(return_value=Response(200, json={"jsonrpc": "2.0", "id": 1}))
    client = _client(tmp_path, monkeypatch)

    assert _call(client, "sf_request", method="GET", path="/cases").status_code == 200
    assert _call(client, "sf_request", method="DELETE", path="/cases/1").status_code == 403


@respx.mock
def test_an_unrecognised_verb_on_a_generic_tool_is_refused_not_waved_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.post(_UPSTREAM).mock(return_value=Response(200, json={"jsonrpc": "2.0", "id": 1}))
    client = _client(tmp_path, monkeypatch)

    assert _call(client, "sf_request", method="TRACE", path="/x").status_code == 403
    assert _call(client, "sf_request", path="/x").status_code == 403  # no method at all


def test_facts_reach_the_decision_context_without_touching_the_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _call(_client(tmp_path, monkeypatch), "delete_case", case_id="500x")

    record = audited[0]
    assert record["context"]["vendor_system"] == "salesforce"
    assert record["context"]["vendor_operation"] == "Case.delete"
    assert record["context"]["crud_action"] == "delete"
    # The mapping alone changes no resource: existing policies keep matching.
    assert record["resource"] == 'Resource::"mcp"'


def test_with_the_flag_on_the_resource_becomes_vendor_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _call(_client(tmp_path, monkeypatch, vendor_scoped_resources=True), "delete_case")

    assert audited[0]["resource"] == 'Resource::"salesforce/Case.delete"'


def test_an_unmapped_tool_lands_on_undeclared_so_it_can_be_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    _call(_client(tmp_path, monkeypatch, vendor_scoped_resources=True), "brand_new_tool")

    assert audited[0]["resource"] == 'Resource::"undeclared"'
    assert "vendor_system" not in audited[0]["context"]


def test_the_caller_cannot_declare_its_own_vendor_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    """Arguments are the caller's; the classification is the operator's. A tool
    argument named like a vendor field must not leak into (or override) it."""
    _call(
        _client(tmp_path, monkeypatch),
        "delete_case",
        crud_action="read",
        vendor_system="harmless",
    )

    assert audited[0]["context"]["crud_action"] == "delete"
    assert audited[0]["context"]["vendor_system"] == "salesforce"


def test_a_tool_mapped_only_under_another_target_is_not_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(app_module.settings, mcp_upstreams={"other": "https://o.internal/mcp"}),
    )
    client = TestClient(
        create_app(PolicyEngine(_policies(tmp_path)), tool_map=ToolMap.from_dict(_TOOLS))
    )

    client.post(
        "/a/agent-a/mcp/other",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "delete_case"}},
    )

    assert "crud_action" not in audited[0]["context"]


def test_without_a_map_nothing_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(
            app_module.settings, mcp_tool_map=None, mcp_upstreams={"sf": _UPSTREAM}
        ),
    )
    client = TestClient(create_app(PolicyEngine(_policies(tmp_path))))

    _call(client, "delete_case")

    assert "vendor_system" not in audited[0]["context"]
    assert audited[0]["resource"] == 'Resource::"mcp"'


def test_a_malformed_configured_map_stops_the_gateway_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module,
        "settings",
        dataclasses.replace(app_module.settings, mcp_tool_map='{"sf": {"t": {"oops": 1}}}'),
    )

    with pytest.raises(ToolMapError):
        create_app(PolicyEngine(_policies(tmp_path)))
