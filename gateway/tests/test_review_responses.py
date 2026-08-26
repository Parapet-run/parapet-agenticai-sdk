"""The gateway's wire surface for a REVIEW decision (docs/adr/0008).

A held call and a blocked call are both refused with HTTP 403 and neither
reaches upstream -- the gateway has no approval workflow of its own. What
must differ is what the caller is TOLD, because that is the only thing an
MCP client or agent runtime can act on: "never going to work, stop asking"
versus "a human is being asked, worth retrying after approval". Collapsing
the two would erase exactly the distinction REVIEW exists to make.

Built against a tmp policy dir rather than the shipped `policies/` bundle
on purpose: these assert the gateway's TRANSLATION of a decision, and must
not start failing the day someone adds or removes a review rule from the
real bundle. Rule content is policies/tests' job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_gateway.server.app import create_app

_MCP_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "bulk_delete", "arguments": {"project": "PROD"}},
}


def _client(tmp_path: Path, *, review: bool) -> TestClient:
    """A bundle that permits tool_call except `bulk_delete`, which is forbidden
    either reviewably or hard."""
    action = '@action("review")\n' if review else ""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "00-base.cedar").write_text(
        'permit(principal, action == Action::"tool_call", resource);'
    )
    (tmp_path / "10-block.cedar").write_text(
        f'@id("bulk_delete_blocked")\n{action}'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has tool_name && context.tool_name == "bulk_delete" };'
    )
    return TestClient(create_app(PolicyEngine(tmp_path)))


@pytest.mark.parametrize(
    "review,expected_decision,expected_code",
    [(True, "review", -32001), (False, "deny", -32000)],
)
def test_mcp_block_is_shaped_by_effect(
    tmp_path: Path, review: bool, expected_decision: str, expected_code: int
) -> None:
    response = _client(tmp_path, review=review).post("/a/probe/mcp", json=_MCP_BODY)

    assert response.status_code == 403
    assert response.headers["x-parapetai-decision"] == expected_decision
    assert response.json()["error"]["code"] == expected_code


def test_review_and_deny_use_distinct_mcp_error_codes(tmp_path: Path) -> None:
    """Guards the pair directly, not just each in isolation: a refactor that
    made both branches return the same constant would pass the parametrised
    test above only if someone also updated its expectations, but would fail
    here unconditionally."""
    review = _client(tmp_path / "r", review=True).post("/a/probe/mcp", json=_MCP_BODY)
    deny = _client(tmp_path / "d", review=False).post("/a/probe/mcp", json=_MCP_BODY)

    assert review.json()["error"]["code"] != deny.json()["error"]["code"]


def test_review_message_says_held_not_blocked(tmp_path: Path) -> None:
    """The message is what a human reads in an agent's error output; "blocked"
    on a call that a colleague can approve sends them to the wrong place."""
    response = _client(tmp_path, review=True).post("/a/probe/mcp", json=_MCP_BODY)

    message = response.json()["error"]["message"]
    assert "Held for human approval" in message
    assert "bulk_delete_blocked" in message  # the policy @id, for the audit trail


def test_deny_message_is_unchanged(tmp_path: Path) -> None:
    response = _client(tmp_path, review=False).post("/a/probe/mcp", json=_MCP_BODY)

    assert response.json()["error"]["message"].startswith("Blocked by governance policy:")


@respx.mock
def test_review_never_reaches_upstream(tmp_path: Path) -> None:
    """The safety property. A held call must not execute -- there is no
    approval yet, so "review" must behave as a refusal at the network edge,
    not an optimistic pass-through."""
    route = respx.post("https://api.openai.com/v1/chat/completions").mock()
    (tmp_path / "00-base.cedar").write_text(
        '@id("model_reviewable")\n@action("review")\n'
        'forbid(principal, action == Action::"model_call", resource);'
    )
    client = TestClient(create_app(PolicyEngine(tmp_path)))

    response = client.post(
        "/a/probe/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 403
    assert not route.called


def test_openai_shaped_review_carries_its_own_error_code(tmp_path: Path) -> None:
    """The non-MCP fallback shape needs the same distinction the JSON-RPC code
    carries, or an OpenAI-SDK caller can only tell review from deny by
    string-matching the message."""
    (tmp_path / "00-base.cedar").write_text(
        '@id("model_reviewable")\n@action("review")\n'
        'forbid(principal, action == Action::"model_call", resource);'
    )
    client = TestClient(create_app(PolicyEngine(tmp_path)))

    response = client.post(
        "/a/probe/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.json()["error"]["code"] == "governance_review_required"
