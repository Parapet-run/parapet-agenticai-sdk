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
