"""parapetai_agent.policy.engine's @stage/@action annotation support.

Verifies the contract PolicyEngine's module docstring states: @stage("pre"
|"post") scopes a policy to evaluate(stage=...)'s pre- or post-call
variant; no @stage annotation means a policy applies to BOTH (never a
special case a bundle author has to remember); @action("alter") +
@alter_with("...") on an ALLOWED decision surface through
Decision.annotations, never on a deny; and a bad @stage/compile failure in
any one of the three variants (full/pre/post) rejects the whole reload,
keeping the previous generation serving (invariant 4).
"""

from __future__ import annotations

from pathlib import Path

from parapetai_agent.policy.engine import PolicyEngine


def _write(policy_dir: Path, name: str, text: str) -> None:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / name).write_text(text)


def test_unstaged_policy_applies_to_both_pre_and_post(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)

    for stage in ("pre", "post", None):
        decision = engine.evaluate(
            principal="a1", action="model_call", resource="openai", context={}, stage=stage
        )
        assert decision.allowed, f"stage={stage}"


def test_stage_pre_forbid_does_not_apply_at_post(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-pre-forbid.cedar",
        '@id("pre_only")\n@stage("pre")\n'
        'forbid(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)

    pre = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context={}, stage="pre"
    )
    post = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context={}, stage="post"
    )
    assert not pre.allowed
    assert post.allowed


def test_stage_post_forbid_does_not_apply_at_pre(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-post-forbid.cedar",
        '@id("post_only")\n@stage("post")\n'
        'forbid(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)

    pre = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context={}, stage="pre"
    )
    post = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context={}, stage="post"
    )
    assert pre.allowed
    assert not post.allowed


def test_no_stage_call_uses_full_unfiltered_set(tmp_path: Path) -> None:
    """A caller that never passes stage= (gateway's app.py, today's every
    existing call site) keeps evaluating the full set, including
    @stage-annotated policies -- zero behavior change."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-post-forbid.cedar",
        '@id("post_only")\n@stage("post")\n'
        'forbid(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)

    decision = engine.evaluate(principal="a1", action="model_call", resource="openai", context={})
    assert not decision.allowed


def test_action_alter_annotations_surface_only_on_allow(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-alter.cedar",
        '@id("alter_rule")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
        'permit(principal, action == Action::"model_call", resource)\n'
        'when { context has response_preview && context.response_preview like "*secret*" };',
    )
    engine = PolicyEngine(tmp_path)

    matched = engine.evaluate(
        principal="a1",
        action="model_call",
        resource="openai",
        context={"response_preview": "the secret is out"},
        stage="post",
    )
    assert matched.allowed
    assert matched.annotations["action"] == "alter"
    assert matched.annotations["alter_with"] == "redact_all"

    # Same content but a stage where the alter rule is out of scope --
    # annotation must not leak in from a policy that wasn't determining.
    at_pre = engine.evaluate(
        principal="a1",
        action="model_call",
        resource="openai",
        context={"response_preview": "the secret is out"},
        stage="pre",
    )
    assert at_pre.allowed
    assert at_pre.annotations == {}


def test_action_alter_never_surfaces_on_a_deny(tmp_path: Path) -> None:
    """A forbid is never softened by an annotation -- @action only ever
    means anything attached to a permit. Confirms a forbid tagged with
    @action (a malformed/unintended bundle) still denies, with no
    annotations leaking through to imply anything was allowed-with-alter."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    _write(
        tmp_path,
        "10-forbid-with-action.cedar",
        '@id("mistagged")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
        'forbid(principal, action == Action::"model_call", resource)\n'
        'when { context has response_preview && context.response_preview like "*secret*" };',
    )
    engine = PolicyEngine(tmp_path)

    decision = engine.evaluate(
        principal="a1",
        action="model_call",
        resource="openai",
        context={"response_preview": "the secret is out"},
        stage="post",
    )
    assert not decision.allowed
    assert decision.annotations == {}


def test_unknown_stage_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)

    decision = engine.evaluate(
        principal="a1", action="model_call", resource="openai", context={}, stage="sideways"
    )
    assert not decision.allowed
    assert decision.errors  # invariant 2: distinguishable from a real policy denial


def test_bad_stage_split_on_reload_keeps_previous_generation(tmp_path: Path) -> None:
    """invariant 4: a reload that would produce a broken stage-filtered
    variant must reject the WHOLE reload, not just the broken variant --
    the previous generation keeps serving every stage, including the ones
    that would have compiled fine on their own."""
    _write(
        tmp_path,
        "00-base.cedar",
        'permit(principal, action == Action::"model_call", resource);',
    )
    engine = PolicyEngine(tmp_path)
    good_generation = engine.status["generation"]

    # A forbid whose `when` clause references an undeclared/invalid
    # comparison the probe-compile step should reject -- appended so the
    # FULL set is fine (base permit matches first for an unrelated action)
    # but this file alone breaks compilation.
    _write(
        tmp_path,
        "10-broken.cedar",
        '@stage("post")\nforbid(principal, action == Action::"model_call", resource)\n'
        "when { this is not valid cedar syntax at all };",
    )
    result = engine.reload()

    assert result["status"] == "failed"
    assert engine.status["generation"] == good_generation
    decision = engine.evaluate(principal="a1", action="model_call", resource="openai", context={})
    assert decision.allowed  # previous (good) generation still serving
