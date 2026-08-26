"""The gateway's half of the approval loop (docs/adr/0009).

The SDK hands a held call back as an exception carrying a ticket. A proxy has
no exception to raise and cannot hold a client connection for a human either,
so the ticket rides on the 403 and the client re-presents it on the retry.

Two properties carry the security argument, and each has a test that fails
when it is removed:

  * **Cedar is evaluated first, every time.** A grant can only unblock a call
    that is STILL a review. If policy hardened in between, the collection is
    never attempted (TestPolicyHardened).
  * **The fingerprint is recomputed from the retried bytes.** Approve a small
    request, retry with a different one, and the control plane refuses -- the
    gateway does not get to choose (TestMutatedRetry).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from parapetai_gateway.server.app import REVIEW_HEADER, create_app

from parapetai_agent.control_plane import ReviewClient
from parapetai_agent.policy.engine import PolicyEngine

CP = "https://control.example"
UPSTREAM = "https://api.openai.com"
SECRET = "test-agent-secret"  # noqa: S105 -- fixture value, not a real credential

_TOOL_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "bulk_delete", "arguments": {"project": "PROD"}},
}


def _policies(tmp_path: Path, *, review: bool = True) -> Path:
    """`bulk_delete` forbidden -- reviewably, or hard."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "00-base.cedar").write_text(
        'permit(principal, action == Action::"tool_call", resource);'
    )
    (tmp_path / "10-block.cedar").write_text(
        f'@id("bulk_delete_blocked")\n{"@action(\"review\")\n" if review else ""}'
        '@review_reason("bulk deletes need a person")\n'
        '@risk_score("high")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has tool_name && context.tool_name == "bulk_delete" };'
    )
    return tmp_path


def _client(tmp_path: Path, *, review: bool = True, connected: bool = True) -> TestClient:
    reviews = (
        ReviewClient(
            control_plane_url=CP, agent_secret=SECRET, agent_id="pa-test", pep_id="pep-1"
        )
        if connected
        else None
    )
    return TestClient(create_app(PolicyEngine(_policies(tmp_path, review=review)), reviews))


@pytest.fixture()
def mcp_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mcp` ships with no default upstream (config.DEFAULT_UPSTREAMS), so a
    forwarded call 502s unless one is configured. Only the tests that assert a
    request actually EXECUTED need this; the refusal tests do not."""
    monkeypatch.setenv("PARAPETAI_MCP_BASE_URL", UPSTREAM)


def _post(client: TestClient, body: dict[str, Any] | None = None, **headers: str) -> Any:
    return client.post(
        "/a/pa-test/mcp", json=body if body is not None else _TOOL_BODY, headers=headers
    )


class TestHeldCallGetsATicket:
    def test_a_review_is_queued_and_the_id_comes_back_on_the_403(
        self, tmp_path: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1", "status": "pending"})
            )
            response = _post(_client(tmp_path))
        assert response.status_code == 403
        assert response.headers["x-parapetai-decision"] == "review"
        assert response.headers[REVIEW_HEADER] == "rv-1"

    def test_an_mcp_client_can_reach_the_ticket_without_headers(
        self, tmp_path: Path
    ) -> None:
        """JSON-RPC clients read the error object and never see headers, so for
        MCP the ticket has to ride in `data` or it is unreachable."""
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            body = _post(_client(tmp_path)).json()
        assert body["error"]["data"]["review_id"] == "rv-1"
        assert body["error"]["data"]["retry_header"] == REVIEW_HEADER

    def test_the_message_says_what_to_do_with_the_ticket(self, tmp_path: Path) -> None:
        """A 403 body is often the only thing an operator sees in an agent's
        logs; a bare id with no hint of what to do with it is a dead end."""
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            body = _post(_client(tmp_path)).json()
        assert REVIEW_HEADER in body["error"]["message"]
        assert "rv-1" in body["error"]["message"]

    def test_the_queued_review_carries_the_policy_authors_detail(
        self, tmp_path: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            _post(_client(tmp_path))
        sent = json.loads(route.calls[0].request.content)
        assert sent["reason"] == "bulk deletes need a person"
        assert sent["risk_score"] == "high"
        assert sent["tool_name"] == "bulk_delete"
        assert "PROD" in sent["args_preview"]


class TestApprovedRetryReachesUpstream:
    @pytest.mark.usefixtures("mcp_upstream")
    def test_collecting_a_grant_forwards_the_request(self, tmp_path: Path) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews/rv-1/collect").mock(
                return_value=httpx.Response(200, json={"status": "consumed", "allowed": True})
            )
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})
            )
            response = _post(_client(tmp_path), **{REVIEW_HEADER: "rv-1"})
        assert response.status_code == 200
        assert upstream.called  # it actually executed

    def test_an_uncollectable_ticket_is_refused_and_re_queued(self, tmp_path: Path) -> None:
        """A client that retries with a ticket that will never come good gets a
        FRESH review, not the dead id echoed back at it."""
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews/rv-old/collect").mock(
                return_value=httpx.Response(409, json={"detail": "review is denied"})
            )
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-new"})
            )
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            response = _post(_client(tmp_path), **{REVIEW_HEADER: "rv-old"})
        assert response.status_code == 403
        assert response.headers[REVIEW_HEADER] == "rv-new"
        assert not upstream.called


class TestMutatedRetry:
    def test_the_fingerprint_is_recomputed_from_the_retried_bytes(
        self, tmp_path: Path
    ) -> None:
        """Approve a small request, retry with a different one: the gateway
        presents the fingerprint of what it is holding NOW, so the control
        plane sees a mismatch. The gateway does not get to choose."""
        captured: list[str] = []
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            router.post(f"{UPSTREAM}/mcp").mock(return_value=httpx.Response(200, json={}))
            client = _client(tmp_path)
            _post(client)  # original request -> queued
            captured.append(json.loads(router.calls[0].request.content)["fingerprint"])

            mutated = json.loads(json.dumps(_TOOL_BODY))
            mutated["params"]["arguments"]["project"] = "EVERYTHING"
            _post(client, mutated)
            captured.append(json.loads(router.calls[-1].request.content)["fingerprint"])

        assert captured[0] != captured[1]

    def test_a_grant_for_a_different_body_does_not_forward(self, tmp_path: Path) -> None:
        """End of the same story: the control plane refuses the mismatch, so
        the mutated retry never reaches upstream."""
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews/rv-1/collect").mock(
                return_value=httpx.Response(409, json={"detail": "fingerprint mismatch"})
            )
            router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-2"})
            )
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            mutated = json.loads(json.dumps(_TOOL_BODY))
            mutated["params"]["arguments"]["project"] = "EVERYTHING"
            response = _post(_client(tmp_path), mutated, **{REVIEW_HEADER: "rv-1"})
        assert response.status_code == 403
        assert not upstream.called


class TestPolicyHardened:
    def test_a_grant_cannot_unblock_a_call_policy_now_denies_outright(
        self, tmp_path: Path
    ) -> None:
        """The ordering argument, tested: Cedar runs first, and collection is
        attempted only while the decision is still `review`. If the rule lost
        its @action("review") between the approval and the retry, the grant is
        never even offered -- an approved-then-hardened call must not execute.
        """
        with respx.mock(assert_all_called=False) as router:
            collect = router.post(f"{CP}/api/v1/reviews/rv-1/collect").mock(
                return_value=httpx.Response(200, json={"status": "consumed", "allowed": True})
            )
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            response = _post(
                _client(tmp_path, review=False), **{REVIEW_HEADER: "rv-1"}
            )
        assert response.status_code == 403
        assert response.headers["x-parapetai-decision"] == "deny"
        assert not collect.called  # never even asked
        assert not upstream.called

    def test_a_hard_deny_is_never_queued(self, tmp_path: Path) -> None:
        with respx.mock(assert_all_called=False) as router:
            submit = router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-nope"})
            )
            response = _post(_client(tmp_path, review=False))
        assert response.status_code == 403
        assert REVIEW_HEADER not in response.headers
        assert not submit.called


class TestDegradedModes:
    def test_no_control_plane_behaves_exactly_as_before(self, tmp_path: Path) -> None:
        """A gateway with no queue refuses a review and offers no ticket --
        unchanged from before approvals existed."""
        response = _post(_client(tmp_path, connected=False))
        assert response.status_code == 403
        assert response.headers["x-parapetai-decision"] == "review"
        assert REVIEW_HEADER not in response.headers

    def test_an_unreachable_control_plane_still_refuses(self, tmp_path: Path) -> None:
        """Fail closed: the local deny stands, the caller simply gets no
        ticket. An outage costs an approval, never an enforcement."""
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews").mock(side_effect=httpx.ConnectError("down"))
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            response = _post(_client(tmp_path))
        assert response.status_code == 403
        assert REVIEW_HEADER not in response.headers
        assert not upstream.called

    def test_an_unreachable_control_plane_cannot_grant_on_retry(self, tmp_path: Path) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{CP}/api/v1/reviews/rv-1/collect").mock(
                side_effect=httpx.ConnectError("down")
            )
            router.post(f"{CP}/api/v1/reviews").mock(side_effect=httpx.ConnectError("down"))
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            response = _post(_client(tmp_path), **{REVIEW_HEADER: "rv-1"})
        assert response.status_code == 403
        assert not upstream.called


class TestContentStaysOut:
    def test_a_model_call_body_is_never_previewed(self, tmp_path: Path) -> None:
        """Invariant 10. A model call's payload is the prompt; it is hashed
        into the fingerprint and never sent."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);'
        )
        (tmp_path / "10-block.cedar").write_text(
            '@id("model_held")\n@action("review")\n'
            'forbid(principal, action == Action::"model_call", resource);'
        )
        reviews = ReviewClient(
            control_plane_url=CP, agent_secret=SECRET, agent_id="pa-test"
        )
        client = TestClient(create_app(PolicyEngine(tmp_path), reviews))
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            client.post(
                "/a/pa-test/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "SSN 123-45-6789"}],
                },
            )
        sent = route.calls[0].request.content.decode()
        assert "123-45-6789" not in sent
        assert json.loads(sent)["args_preview"] is None
        assert json.loads(sent)["fingerprint"]


class TestMonitorMode:
    @pytest.mark.usefixtures("mcp_upstream")
    def test_monitor_mode_does_not_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is blocked in monitor mode, so there is no held call to
        approve -- queueing would fill an operator's queue with requests that
        already went through."""
        from parapetai_gateway.config import Settings

        # Settings is a frozen dataclass and `enforcing` is a derived property,
        # so the instance cannot be mutated. Shadow the property on the CLASS;
        # monkeypatch restores it.
        monkeypatch.setattr(Settings, "enforcing", False)
        with respx.mock(assert_all_called=False) as router:
            submit = router.post(f"{CP}/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            upstream = router.post(f"{UPSTREAM}/mcp").mock(
                return_value=httpx.Response(200, json={})
            )
            response = _post(_client(tmp_path))
        assert response.status_code == 200
        assert upstream.called
        assert not submit.called
