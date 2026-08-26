"""parapetai_agent.policy.engine's REVIEW outcome (docs/adr/0008).

Verifies the contract PolicyEngine's module docstring states: `@action(
"review")` on a `forbid` makes that deny escalatable to a human, surfacing
as Decision.effect == "review" while Decision.allowed stays False; and that
each of the three fail-closed guards in _is_reviewable independently keeps
a deny HARD -- unanimity across determining policies, a non-empty
determining set, and no evaluation errors.

The unanimity tests are the security-critical ones. cedarpy returns EVERY
matching forbid in diagnostics.reasons (verified directly: two matching
forbids come back as ['policy1', 'policy0'] -- both present, and not in
source order). If a hard forbid matching alongside a reviewable one were
allowed to resolve as "review", a human approval could authorise an action
that some rule said "never" to. That is a privilege-escalation bug, not a
cosmetic one, which is why it is tested from several directions.
"""

from __future__ import annotations

from pathlib import Path

from parapetai_agent.policy.engine import REVIEW_ACTION, Decision, PolicyEngine, _is_reviewable

BASE_PERMIT = 'permit(principal, action == Action::"tool_call", resource);'


def _write(policy_dir: Path, name: str, text: str) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / name).write_text(text)


def _forbid(policy_id: str, *, review: bool, tool: str = "bulk_delete", extra: str = "") -> str:
    """One forbid on `tool`, optionally carrying the review affordance."""
    annotations = f'@id("{policy_id}")\n'
    if review:
        annotations += f'@action("{REVIEW_ACTION}")\n'
    annotations += extra
    return (
        f"{annotations}"
        f'forbid(principal, action == Action::"tool_call", resource)\n'
        f'when {{ context has tool_name && context.tool_name == "{tool}" }};'
    )


def _decide(engine: PolicyEngine, tool: str = "bulk_delete", stage: str | None = None) -> Decision:
    return engine.evaluate(
        principal="a1",
        action="tool_call",
        resource="jira",
        context={"tool_name": tool},
        stage=stage,
    )


# ── the happy path ───────────────────────────────────────────────────


def test_review_forbid_yields_review_effect(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-review.cedar", _forbid("bulk_delete_reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "review"
    assert decision.requires_review is True


def test_review_is_never_an_allow(tmp_path: Path) -> None:
    """The property that makes REVIEW safe to add to an existing deployment:
    a caller that only ever checks `allowed` blocks a review exactly as it
    blocked a deny. If this ever flips, every pre-REVIEW integration silently
    starts executing held calls."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-review.cedar", _forbid("bulk_delete_reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.allowed is False


def test_unmatched_tool_still_allowed(tmp_path: Path) -> None:
    """A review rule must not disturb traffic it does not match."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-review.cedar", _forbid("bulk_delete_reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path), tool="get_issue")

    assert (decision.effect, decision.allowed, decision.requires_review) == ("allow", True, False)


# ── guard 1: unanimity ───────────────────────────────────────────────


def test_hard_forbid_alongside_reviewable_stays_a_hard_deny(tmp_path: Path) -> None:
    """THE security test. Both forbids match the same request; one offers no
    review affordance. Unanimity is what stops a human approval from
    overriding the rule that said "never"."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-review.cedar", _forbid("reviewable", review=True))
    _write(tmp_path, "20-hard.cedar", _forbid("hard_never", review=False))

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "deny"
    assert decision.requires_review is False
    # Both forbids really did determine this decision -- otherwise the test
    # would pass for the wrong reason (only one policy matching at all).
    assert len(decision.determining_policies) == 2


def test_hard_forbid_wins_regardless_of_file_order(tmp_path: Path) -> None:
    """cedarpy returned reasons as ['policy1', 'policy0'] in the probe -- not
    source order. Reversing which file declares the hard forbid must not
    change the outcome, or _is_reviewable is reading only the first reason."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-hard.cedar", _forbid("hard_never", review=False))
    _write(tmp_path, "20-review.cedar", _forbid("reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "deny"


def test_two_reviewable_forbids_stay_reviewable(tmp_path: Path) -> None:
    """Unanimity means ALL, not EXACTLY ONE -- two reviewable rules matching
    the same call is still a reviewable call."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-a.cedar", _forbid("reviewable_a", review=True))
    _write(tmp_path, "20-b.cedar", _forbid("reviewable_b", review=True))

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "review"
    assert len(decision.determining_policies) == 2


# ── guard 2: a bare default-deny is never reviewable ──────────────────


def test_default_deny_with_no_matching_policy_is_not_reviewable(tmp_path: Path) -> None:
    """No permit, no forbid -- Cedar's default-deny, empty determining set.
    Reviewability must be opted into by a rule, never inferred from the
    absence of one."""
    _write(tmp_path, "10-review.cedar", _forbid("reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path), tool="something_unmatched")

    assert decision.determining_policies == ()
    assert decision.effect == "deny"
    assert decision.requires_review is False


# ── guard 3: fail-closed denies stay hard ────────────────────────────


def test_evaluation_errors_force_a_hard_deny() -> None:
    """Invariant 2: a fail-closed deny stays distinguishable from a policy
    decision. Even with a unanimously reviewable determining set, an
    evaluation error must not present as "a human can approve this" -- there
    is no policy intent behind it to approve.

    Unit-level because a bundle that both errors AND resolves a determining
    review forbid is not reliably constructible from Cedar source; the guard
    is a documented branch of a pure function, so it is tested as one.
    """
    annotations = {"policy0": {"action": REVIEW_ACTION}}

    assert _is_reviewable(("policy0",), annotations, ()) is True
    assert _is_reviewable(("policy0",), annotations, ("boom",)) is False


def test_unknown_stage_denies_and_is_not_reviewable(tmp_path: Path) -> None:
    """evaluate()'s programming-error path builds a Decision directly rather
    than going through the review resolver -- it must still report as a hard
    deny, not a review."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)

    decision = _decide(PolicyEngine(tmp_path), stage="sideways")

    assert decision.effect == "deny"
    assert decision.requires_review is False
    assert decision.errors  # invariant 2: distinguishable from a policy deny


def test_determining_policy_missing_from_annotation_map_is_not_reviewable() -> None:
    """A determining id absent from the map resolves to {} -- whose "action"
    is None, not "review". It must force a hard deny rather than be skipped
    by the all() quantifier."""
    annotations = {"policy0": {"action": REVIEW_ACTION}}

    assert _is_reviewable(("policy0", "policy_absent"), annotations, ()) is False


def test_annotation_map_miss_alone_denies() -> None:
    assert _is_reviewable(("policy_absent",), {}, ()) is False


# ── annotations: the channel to an approvals UI ──────────────────────


def test_review_surfaces_author_annotations(tmp_path: Path) -> None:
    """@review_reason/@risk_score are the only way a policy author's
    reviewer-facing detail reaches an approvals queue. PolicyEngine has no
    opinion on what they mean -- it just must not drop them."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(
        tmp_path,
        "10-review.cedar",
        _forbid(
            "bulk_delete_reviewable",
            review=True,
            extra='@review_reason("bulk destructive operation")\n@risk_score("92")\n',
        ),
    )

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.annotations["review_reason"] == "bulk destructive operation"
    assert decision.annotations["risk_score"] == "92"


def test_hard_deny_still_surfaces_no_annotations(tmp_path: Path) -> None:
    """The pre-existing ADR 0006 contract: a forbid with no review affordance
    is never softened OR explained by an annotation."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(
        tmp_path,
        "10-hard.cedar",
        _forbid("hard_never", review=False, extra='@review_reason("should not leak")\n'),
    )

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "deny"
    assert decision.annotations == {}


def test_review_reason_names_the_policy_id_not_the_positional_id(tmp_path: Path) -> None:
    """cedarpy's policy0/policy1 ids shift whenever a bundle gains or loses a
    rule -- useless in an audit record or an approvals queue."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-review.cedar", _forbid("bulk_delete_reviewable", review=True))

    decision = _decide(PolicyEngine(tmp_path))

    assert "bulk_delete_reviewable" in decision.reason
    assert "policy" not in decision.reason


# ── interaction with the existing annotation vocabulary ──────────────


def test_review_on_a_permit_is_inert(tmp_path: Path) -> None:
    """Mirrors ADR 0006's "@action('alter') on a forbid is inert": a
    misplaced annotation is simply ignored, never validated into an error and
    never changing the decision."""
    _write(
        tmp_path,
        "00-base.cedar",
        f'@id("permit_with_review")\n@action("{REVIEW_ACTION}")\n{BASE_PERMIT}',
    )

    decision = _decide(PolicyEngine(tmp_path))

    assert decision.effect == "allow"
    assert decision.requires_review is False


def test_review_respects_stage_filtering(tmp_path: Path) -> None:
    """Each @stage variant builds its OWN annotation map from its own
    post-filter survival order (cedarpy renumbers positionally). A review
    forbid scoped to "pre" must resolve as review at pre and be absent at
    post -- if the per-variant maps misaligned, this would surface as a hard
    deny at pre (annotation lookup miss) instead."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(
        tmp_path,
        "10-review.cedar",
        '@stage("pre")\n' + _forbid("pre_only_reviewable", review=True),
    )
    engine = PolicyEngine(tmp_path)

    assert _decide(engine, stage="pre").effect == "review"
    assert _decide(engine, stage="post").effect == "allow"


# ── audit ────────────────────────────────────────────────────────────


def test_audit_record_carries_review_effect_and_annotations(tmp_path: Path) -> None:
    """An approvals queue is built from the audit stream; if the record said
    "deny" the held call would be indistinguishable from a blocked one."""
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(
        tmp_path,
        "10-review.cedar",
        _forbid("bulk_delete_reviewable", review=True, extra='@risk_score("92")\n'),
    )

    record = _decide(PolicyEngine(tmp_path)).to_audit_record(
        principal='Agent::"a1"',
        action="tool_call",
        resource='Resource::"jira"',
        context={"tool_name": "bulk_delete"},
    )

    assert record["decision"] == "review"
    assert record["annotations"]["risk_score"] == "92"


# ── regression: a bundle with no review annotations is unchanged ─────


def test_bundle_without_review_annotations_behaves_exactly_as_before(tmp_path: Path) -> None:
    _write(tmp_path, "00-base.cedar", BASE_PERMIT)
    _write(tmp_path, "10-hard.cedar", _forbid("hard_never", review=False))
    engine = PolicyEngine(tmp_path)

    denied = _decide(engine)
    allowed = _decide(engine, tool="get_issue")

    assert (denied.effect, denied.allowed) == ("deny", False)
    assert denied.reason == "denied: no permit matched or forbid applied"
    assert (allowed.effect, allowed.allowed) == ("allow", True)
