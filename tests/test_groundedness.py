"""QUAL-1 groundedness scorer.

The scorer is a dependency-free faithfulness proxy: it must pass an answer that
the source supports, fail one that invents content, and treat a fabricated
NUMBER (wrong price/date) as a hard contradiction regardless of word overlap --
the failure mode that matters most for an agent moving money or quoting policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from parapetai_agent.groundedness import GroundednessConfig, score_groundedness

SOURCE = (
    "Our refund window is 30 days from delivery. Orders over $500 need manager approval. "
    "The warehouse is in Austin, Texas and ships within two business days."
)


def test_grounded_answer_passes() -> None:
    r = score_groundedness("The refund window is 30 days from delivery.", SOURCE)
    assert r.grounded is True
    assert r.score >= 0.6
    assert r.contradiction is False


def test_fabricated_content_fails() -> None:
    # "loyalty points" / "expedited shipping to Canada" appear nowhere in source
    r = score_groundedness(
        "You also earn loyalty points and get expedited shipping to Canada.", SOURCE
    )
    assert r.grounded is False
    assert r.unsupported_claims  # names which claim was unsupported (display only)


def test_wrong_number_is_a_contradiction() -> None:
    # source says 30 days; answer says 90 -> fabricated number, hard fail even
    # though every other word overlaps
    r = score_groundedness("The refund window is 90 days from delivery.", SOURCE)
    assert r.contradiction is True
    assert r.grounded is False
    assert "numeric" in r.reason


def test_number_formatting_is_not_a_contradiction() -> None:
    src = "Orders over $1,000 require approval."
    r = score_groundedness("Orders over $1000 require approval.", src)
    assert r.contradiction is False
    assert r.grounded is True


def test_empty_response_is_grounded() -> None:
    r = score_groundedness("", SOURCE)
    assert r.grounded is True and r.score == 1.0


def test_threshold_controls_strictness() -> None:
    # one supported claim + one unsupported -> score 0.5
    answer = "The refund window is 30 days. We also price-match competitors."
    lenient = score_groundedness(answer, SOURCE, threshold=0.5)
    strict = score_groundedness(answer, SOURCE, threshold=0.9)
    assert lenient.score == 0.5
    assert lenient.grounded is True
    assert strict.grounded is False


def test_unknown_backend_falls_back_to_lexical() -> None:
    # a bundle naming a backend this build doesn't ship still CHECKS (degraded),
    # never silently turns groundedness off
    r = score_groundedness("The refund window is 30 days.", SOURCE, backend="does-not-exist")
    assert r.backend == "lexical"
    assert r.grounded is True


def test_verdict_is_content_free_shaped() -> None:
    # the fields that flow to a decision are a score + booleans, not raw text;
    # unsupported_claims is display-only and stays SDK-side
    r = score_groundedness("Ships from Austin, Texas.", SOURCE)
    assert isinstance(r.score, float)
    assert isinstance(r.grounded, bool)
    assert r.backend == "lexical"


# ── GroundednessConfig: bundle load + fail-closed post evaluation ─────────
def _config(threshold: float = 0.6, backend: str = "lexical") -> GroundednessConfig:
    cfg = GroundednessConfig()
    cfg.load_from_bundle(
        {
            "groundedness.json": json.dumps(
                [
                    {
                        "library_id": "groundedness-check",
                        "backend": backend,
                        "threshold": threshold,
                        "context_key": "content_checks_ungrounded",
                    }
                ]
            )
        }
    )
    return cfg


class TestGroundednessConfig:
    def test_empty_bundle_is_inactive(self) -> None:
        cfg = GroundednessConfig()
        cfg.load_from_bundle({})
        assert cfg.active is False
        assert cfg.evaluate_post("x", "y").context == {}

    def test_malformed_json_is_inactive_not_a_crash(self) -> None:
        cfg = GroundednessConfig()
        cfg.load_from_bundle({"groundedness.json": "{ not json"})
        assert cfg.active is False

    def test_grounded_response_sets_key_false(self) -> None:
        cfg = _config()
        res = cfg.evaluate_post("The refund window is 30 days.", SOURCE)
        assert res.errors == ()
        assert res.context["content_checks_ungrounded"] is False

    def test_ungrounded_response_sets_key_true(self) -> None:
        cfg = _config()
        res = cfg.evaluate_post("The refund window is 90 days and you earn points.", SOURCE)
        assert res.context["content_checks_ungrounded"] is True

    def test_backend_error_fails_closed(self) -> None:
        # hhem backend isn't installed -> evaluate_post captures the error so the
        # caller (maf.py) denies, rather than letting it read as grounded.
        cfg = _config(backend="hhem")
        res = cfg.evaluate_post("anything", SOURCE)
        assert res.errors  # non-empty -> hard deny upstream
        assert "content_checks_ungrounded" not in res.context


# ── End-to-end enforcement through the real middleware ───────────────────
class TestGroundednessEnforcement:
    @staticmethod
    def _policy_dir(tmp_path: Path) -> Path:
        d = tmp_path / "pol"
        d.mkdir()
        (d / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);\n'
        )
        # exactly what control-plane's _forbid_ungrounded renders
        (d / "45-tier2.cedar").write_text(
            '@id("content-check-groundedness")\n'
            '@stage("post")\n'
            'forbid (principal, action == Action::"model_call", resource)\n'
            "when {\n  context has content_checks_ungrounded &&\n"
            "  context.content_checks_ungrounded == true\n};\n"
        )
        return d

    def _mw(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        from parapetai_agent.identity import Caller
        from parapetai_agent.maf import ParapetChatMiddleware
        from parapetai_agent.policy.engine import PolicyEngine

        engine = PolicyEngine(self._policy_dir(tmp_path))
        caller = Caller(agent_id="g-test", tenant="default")
        return ParapetChatMiddleware(engine, caller, groundedness=_config())

    @staticmethod
    def _ctx(answer: str):  # type: ignore[no-untyped-def]
        from agent_framework import ChatContext, ChatResponse, Message
        from agent_framework.openai import OpenAIChatCompletionClient

        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", [SOURCE])],  # the source the answer must be grounded in
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", [answer])])

        return ctx, call_next

    async def test_grounded_answer_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        mw = self._mw(tmp_path)
        ctx, call_next = self._ctx("The refund window is 30 days from delivery.")
        await mw.process(ctx, call_next)  # no raise -> allowed

    async def test_hallucinated_answer_is_denied_post_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from parapetai_agent.maf import GovernanceDenied

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        mw = self._mw(tmp_path)
        ctx, call_next = self._ctx("The refund window is 90 days and you also earn loyalty points.")
        with pytest.raises(GovernanceDenied) as exc:
            await mw.process(ctx, call_next)
        assert exc.value.decision.effect == "deny"
        assert not exc.value.decision.allowed
