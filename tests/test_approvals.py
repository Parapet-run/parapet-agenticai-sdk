"""The approval loop, SDK side (docs/adr/0009).

ADR 0008 gave the engine a third outcome; nothing could resolve one. These
tests cover what a PEP now does with a held call: escalate it, hand the caller
a ticket, and collect a grant that a human answered -- without ever letting the
control plane onto the decision path.

The backward-compatibility test (TestHeldCallIsStillADeny) is the important
one. GovernanceReviewRequired subclasses GovernanceDenied for the same reason
Decision.allowed stays False for a review: every integration written before
approvals existed must keep blocking a held call, and upgrading the SDK must
not start executing one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from parapetai_agent import GovernanceDenied, GovernanceReviewRequired, Governor
from parapetai_agent.control_plane import ReviewClient, review_fingerprint
from parapetai_agent.policy.engine import REVIEW_ACTION

CP = "https://control.example"
BASE_PERMIT = 'permit(principal, action == Action::"tool_call", resource);'
MODEL_PERMIT = 'permit(principal, action == Action::"model_call", resource);'
# Fixture value for a PEP bearer secret, not a real credential.
TEST_SECRET = "test-agent-secret"  # noqa: S105


def _policy_dir(tmp_path: Path) -> Path:
    """A bundle whose only forbid is reviewable: `bulk_delete` is held for a
    human, everything else is permitted."""
    d = tmp_path / "policies"
    d.mkdir(parents=True, exist_ok=True)
    (d / "00-base.cedar").write_text(f"{BASE_PERMIT}\n{MODEL_PERMIT}")
    (d / "10-review.cedar").write_text(
        '@id("tools-bulk-delete")\n'
        f'@action("{REVIEW_ACTION}")\n'
        '@review_reason("bulk deletes need a person")\n'
        '@risk_score("high")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has tool_name && context.tool_name == "bulk_delete" };'
    )
    (d / "20-hard.cedar").write_text(
        '@id("tools-never")\n'
        'forbid(principal, action == Action::"tool_call", resource)\n'
        'when { context has tool_name && context.tool_name == "drop_database" };'
    )
    return d


def _gov(tmp_path: Path, *, connected: bool = True) -> Governor:
    gov = Governor.from_policy_dir(_policy_dir(tmp_path))
    if connected:
        gov._reviews = ReviewClient(
            control_plane_url=CP, agent_secret=TEST_SECRET, agent_id="pa-test", pep_id="pep-1"
        )
    return gov


def _mock_submit(review_id: str = "rv-abc123") -> respx.Router:
    router = respx.mock(base_url=CP, assert_all_called=False)
    router.post("/api/v1/reviews").mock(
        return_value=httpx.Response(
            200, json={"review_id": review_id, "status": "pending", "allowed": False}
        )
    )
    return router


class TestHeldCallIsStillADeny:
    """The property that made this safe to add to a running deployment."""

    def test_a_review_raises_and_the_tool_never_runs(self, tmp_path: Path) -> None:
        with _mock_submit():
            with pytest.raises(GovernanceReviewRequired) as exc:
                _gov(tmp_path).authorize_tool("bulk_delete", {"n": 1000})
        assert exc.value.decision.effect == "review"
        assert exc.value.decision.allowed is False

    def test_code_written_before_approvals_still_blocks_it(self, tmp_path: Path) -> None:
        """An `except GovernanceDenied:` written when only allow/deny existed
        must keep catching a held call. If this ever fails, upgrading the SDK
        silently starts executing calls a policy held."""
        caught = False
        with _mock_submit():
            try:
                _gov(tmp_path).authorize_tool("bulk_delete", {"n": 1000})
            except GovernanceDenied:
                caught = True
        assert caught is True

    def test_a_hard_forbid_is_not_reviewable(self, tmp_path: Path) -> None:
        """Unanimity (ADR 0008): a forbid with no review affordance stays a
        hard deny, and must never be queued for a human to wave through."""
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post("/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-nope"})
            )
            with pytest.raises(GovernanceDenied) as exc:
                _gov(tmp_path).authorize_tool("drop_database", {})
        assert not isinstance(exc.value, GovernanceReviewRequired)
        assert exc.value.decision.effect == "deny"
        assert not route.called  # never even offered to a human

    def test_an_allowed_call_is_unaffected(self, tmp_path: Path) -> None:
        d = _gov(tmp_path).authorize_tool("lookup", {"id": 1})
        assert d.allowed is True


class TestUnreachableControlPlaneCannotSoften:
    def test_no_control_plane_means_no_ticket_but_still_a_deny(self, tmp_path: Path) -> None:
        """A locally-constructed Governor has no queue. The call is still
        held -- a review is a deny -- there is simply nobody to ask."""
        with pytest.raises(GovernanceReviewRequired) as exc:
            _gov(tmp_path, connected=False).authorize_tool("bulk_delete", {"n": 1})
        assert exc.value.review_id is None
        assert exc.value.decision.allowed is False

    def test_a_network_failure_degrades_to_an_unqueued_deny(self, tmp_path: Path) -> None:
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post("/api/v1/reviews").mock(side_effect=httpx.ConnectError("down"))
            with pytest.raises(GovernanceReviewRequired) as exc:
                _gov(tmp_path).authorize_tool("bulk_delete", {"n": 1})
        assert exc.value.review_id is None

    def test_waiting_on_an_unqueued_review_refuses_immediately(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path, connected=False)
        try:
            gov.authorize_tool("bulk_delete", {"n": 1})
        except GovernanceReviewRequired as held:
            assert gov.wait_for_approval(held, timeout=30.0) is False  # returns at once


class TestWhatReachesTheApprover:
    def test_the_policy_authors_detail_travels_with_the_call(self, tmp_path: Path) -> None:
        """@review_reason and @risk_score are the only channel by which a held
        call explains itself to a human (ADR 0008)."""
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post("/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-1"})
            )
            with pytest.raises(GovernanceReviewRequired):
                _gov(tmp_path).authorize_tool("bulk_delete", {"scope": "all-incidents"})
        sent = json.loads(route.calls[0].request.content)
        assert sent["reason"] == "bulk deletes need a person"
        assert sent["risk_score"] == "high"
        assert sent["policy_id"]
        assert sent["tool_name"] == "bulk_delete"
        assert "all-incidents" in sent["args_preview"]

    def test_a_model_call_is_fingerprinted_but_never_previewed(self, tmp_path: Path) -> None:
        """Invariant 10: the "arguments" of a model call are the prompt, and
        prompt content never reaches the control plane unless someone opts in.
        A digest is not content, so the grant is still bound to this exact
        prompt."""
        d = _policy_dir(tmp_path)
        (d / "30-model.cedar").write_text(
            '@id("model-review")\n'
            f'@action("{REVIEW_ACTION}")\n'
            'forbid(principal, action == Action::"model_call", resource);'
        )
        gov = Governor.from_policy_dir(d)
        gov._reviews = ReviewClient(
            control_plane_url=CP, agent_secret=TEST_SECRET, agent_id="pa-test"
        )
        sensitive_prompt = "my patient's SSN is 123-45-6789"
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post("/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-2"})
            )
            with pytest.raises(GovernanceReviewRequired):
                gov.check_input(sensitive_prompt)
        body = route.calls[0].request.content.decode()
        assert "123-45-6789" not in body
        assert "SSN" not in body
        assert json.loads(body)["args_preview"] is None
        assert json.loads(body)["fingerprint"]  # still bound to this prompt


class TestFingerprintBinding:
    def test_the_same_call_fingerprints_the_same_way(self) -> None:
        """Stable across retries and processes, or a resubmitting agent would
        queue a duplicate review instead of joining its own."""
        a = review_fingerprint(agent_id="pa-1", action="tool_call", tool_name="t", args={"a": 1})
        b = review_fingerprint(agent_id="pa-1", action="tool_call", tool_name="t", args={"a": 1})
        assert a == b

    def test_key_order_does_not_change_it(self) -> None:
        a = review_fingerprint(agent_id="pa-1", action="tool_call", args={"x": 1, "y": 2})
        b = review_fingerprint(agent_id="pa-1", action="tool_call", args={"y": 2, "x": 1})
        assert a == b

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"agent_id": "pa-2", "action": "tool_call", "tool_name": "t", "args": {"a": 1}},
            {"agent_id": "pa-1", "action": "model_call", "tool_name": "t", "args": {"a": 1}},
            {"agent_id": "pa-1", "action": "tool_call", "tool_name": "other", "args": {"a": 1}},
            {"agent_id": "pa-1", "action": "tool_call", "tool_name": "t", "args": {"a": 2}},
        ],
    )
    def test_any_difference_changes_it(self, kwargs: dict[str, Any]) -> None:
        """This is what stops "close INC-42" being replayed onto INC-43 -- the
        control plane compares these strings and refuses a mismatch."""
        base = review_fingerprint(
            agent_id="pa-1", action="tool_call", tool_name="t", args={"a": 1}
        )
        assert review_fingerprint(**kwargs) != base

    def test_unserialisable_arguments_still_fingerprint(self) -> None:
        """Computed on the deny path -- raising here would turn a held call
        into a crash."""
        assert review_fingerprint(agent_id="pa-1", action="tool_call", args={"o": object()})


class TestWaitForApproval:
    def _held(self, tmp_path: Path, gov: Governor) -> GovernanceReviewRequired:
        with _mock_submit():
            try:
                gov.authorize_tool("bulk_delete", {"n": 1})
            except GovernanceReviewRequired as exc:
                return exc
        raise AssertionError("expected a held call")

    def test_approved_and_collected_returns_true(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(200, json={"status": "consumed", "allowed": True})
            )
            assert gov.wait_for_approval(held, timeout=5.0, poll_interval=0.01) is True

    def test_it_presents_the_fingerprint_of_the_call_it_is_waiting_on(
        self, tmp_path: Path
    ) -> None:
        """Collecting has to prove which call it is collecting for, or a grant
        could be spent on a neighbouring action."""
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(200, json={"status": "consumed", "allowed": True})
            )
            gov.wait_for_approval(held, timeout=5.0, poll_interval=0.01)
        assert json.loads(route.calls[0].request.content)["fingerprint"] == held.fingerprint
        assert held.fingerprint == review_fingerprint(
            agent_id="pa-test", action="tool_call", tool_name="bulk_delete", args={"n": 1}
        )

    def test_a_denial_stops_the_wait_immediately(self, tmp_path: Path) -> None:
        """Nobody is coming to change a denied review; polling it until the
        timeout only delays the caller's own error path."""
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(200, json={"status": "denied", "allowed": False})
            )
            # A long timeout that must NOT be waited out.
            assert gov.wait_for_approval(held, timeout=600.0, poll_interval=0.01) is False
        assert route.call_count == 1

    def test_an_expired_review_stops_the_wait(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(200, json={"status": "expired", "allowed": False})
            )
            assert gov.wait_for_approval(held, timeout=600.0, poll_interval=0.01) is False

    def test_it_keeps_polling_while_pending_then_succeeds(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        responses = [
            httpx.Response(200, json={"status": "pending", "allowed": False}),
            httpx.Response(200, json={"status": "pending", "allowed": False}),
            httpx.Response(200, json={"status": "consumed", "allowed": True}),
        ]
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                side_effect=responses
            )
            assert gov.wait_for_approval(held, timeout=5.0, poll_interval=0.01) is True
        assert route.call_count == 3

    def test_timing_out_is_not_an_approval(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(200, json={"status": "pending", "allowed": False})
            )
            assert gov.wait_for_approval(held, timeout=0.05, poll_interval=0.01) is False

    def test_a_control_plane_error_is_not_an_approval(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                side_effect=httpx.ConnectError("down")
            )
            assert gov.wait_for_approval(held, timeout=0.05, poll_interval=0.01) is False

    def test_a_409_from_the_control_plane_is_not_an_approval(self, tmp_path: Path) -> None:
        """Fingerprint mismatch / already consumed. The SDK must read a refusal
        as "no grant", never fall through to allowed."""
        gov = _gov(tmp_path)
        held = self._held(tmp_path, gov)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post(f"/api/v1/reviews/{held.review_id}/collect").mock(
                return_value=httpx.Response(409, json={"detail": "fingerprint mismatch"})
            )
            assert gov.wait_for_approval(held, timeout=0.05, poll_interval=0.01) is False


class TestRaiseOnDenyFalse:
    def test_it_returns_the_decision_without_queueing(self, tmp_path: Path) -> None:
        """Decision is frozen, so a non-raising return has nowhere to carry a
        review_id -- queueing anyway would leave rows in an operator's queue
        that the caller can never poll or resolve."""
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            route = router.post("/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-x"})
            )
            d = _gov(tmp_path).authorize_tool("bulk_delete", {"n": 1}, raise_on_deny=False)
        assert d.effect == "review"
        assert d.allowed is False
        assert not route.called

    def test_the_caller_can_queue_it_explicitly(self, tmp_path: Path) -> None:
        gov = _gov(tmp_path)
        with respx.mock(base_url=CP, assert_all_called=False) as router:
            router.post("/api/v1/reviews").mock(
                return_value=httpx.Response(200, json={"review_id": "rv-y"})
            )
            d = gov.authorize_tool("bulk_delete", {"n": 1}, raise_on_deny=False)
            review_id, fingerprint = gov.request_approval(
                d, action="tool_call", tool_name="bulk_delete", args={"n": 1}
            )
        assert review_id == "rv-y"
        assert fingerprint == review_fingerprint(
            agent_id="pa-test", action="tool_call", tool_name="bulk_delete", args={"n": 1}
        )
