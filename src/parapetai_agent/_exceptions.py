"""The one governance exception, defined at base level.

Both the framework-neutral facade (parapetai_agent.govern) and the MAF adapter
(parapetai_agent.maf) raise this, so a denial is a single catchable type no
matter which entry point produced it -- and it's importable from the base
install, with no agent-framework dependency.
"""

from __future__ import annotations

from parapetai_agent.policy.engine import Decision


class GovernanceDenied(Exception):
    """Raised when a governance decision denies. Carries the Cedar `decision`
    (verdict, reason, determining policy, stage) so a caller can inspect why."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__(f"Blocked by governance policy: {decision.reason}")


class GovernanceReviewRequired(GovernanceDenied):
    """Raised when a call was HELD for a human rather than refused outright.

    **A subclass of GovernanceDenied, deliberately.** Every `except
    GovernanceDenied:` written before approvals existed keeps blocking a held
    call, and no caller can start executing one by upgrading the SDK. It is the
    same reasoning that keeps `Decision.allowed` False for a review (ADR 0008,
    invariant 11): the affordance is additive, and the failure mode of an
    unaware caller is "blocked", never "executed".

    `review_id` is None when the call was reviewable but the control plane
    could not be reached to queue it. That is still a deny -- there is simply
    no human to wait for, so there is nothing to poll and
    `Governor.wait_for_approval()` will refuse it immediately.
    """

    def __init__(
        self, decision: Decision, *, review_id: str | None = None, fingerprint: str | None = None
    ) -> None:
        self.decision = decision
        self.review_id = review_id
        self.fingerprint = fingerprint
        held = f" (review {review_id})" if review_id else " (not queued: control plane unreachable)"
        Exception.__init__(self, f"Held for approval: {decision.reason}{held}")
