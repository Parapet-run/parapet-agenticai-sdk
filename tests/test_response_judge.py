"""SLM-judge response evaluation (parapetai_agent/response_judge.py).

The real model is an external endpoint, so these tests inject a fake backend
into the JUDGES registry and assert the contract the runtime depends on: a
verdict is parsed to pass/fail, a FAILED response sets its context key True (so
Cedar can forbid on it), and a backend that raises surfaces as an error the
caller turns into a hard deny (fail-closed). Ambiguous verdicts fail closed too.
"""

from __future__ import annotations

import json

import pytest
from parapetai_agent import response_judge as rj


class TestParseVerdict:
    def test_json_pass_and_fail(self) -> None:
        assert rj._parse_verdict('{"pass": true, "score": 0.9}', 0.5).passed is True
        assert rj._parse_verdict('{"pass": false, "score": 0.1}', 0.5).passed is False

    def test_free_text(self) -> None:
        assert rj._parse_verdict("PASS", 0.5).passed is True
        assert rj._parse_verdict("This response VIOLATES the policy.", 0.5).passed is False

    def test_unparseable_fails_closed(self) -> None:
        # fail-toward-flagging: an ambiguous judge answer is a possible violation
        assert rj._parse_verdict("hmm, not sure", 0.5).passed is False


class TestJudgeConfig:
    @pytest.fixture(autouse=True)
    def _fake_backend(self):  # type: ignore[no-untyped-def]
        seen: dict[str, str] = {}

        def fake(response, rubric, threshold, options):  # noqa: ANN001, ANN202
            seen["response"] = response
            seen["rubric"] = rubric
            if "boom" in options.get("model", ""):
                raise RuntimeError("endpoint down")
            passed = "safe" in response  # the fake "judge"
            return rj.JudgeResult(passed, 1.0 if passed else 0.0, "fake")

        rj.JUDGES["fake"] = fake
        yield seen
        rj.JUDGES.pop("fake", None)

    def _bundle(self, **over: object) -> dict[str, str]:
        entry = {
            "library_id": "policy-judge",
            "context_key": "content_checks_slm_failed",
            "backend": "fake",
            "rubric": "Does the response follow the refund policy?",
            "model": "small-model",
            "base_url": "http://localhost:1234/v1",
            **over,
        }
        return {rj.CONFIG_FILENAME: json.dumps([entry])}

    def test_inactive_until_bundle_selects_a_judge(self) -> None:
        cfg = rj.JudgeConfig()
        assert cfg.active is False
        assert cfg.evaluate_post("anything").context == {}

    def test_pass_sets_key_false_fail_sets_true(self, _fake_backend) -> None:  # type: ignore[no-untyped-def]
        cfg = rj.JudgeConfig()
        cfg.load_from_bundle(self._bundle())
        assert cfg.active is True

        ok = cfg.evaluate_post("this is a safe answer")
        assert ok.errors == () and ok.context["content_checks_slm_failed"] is False

        bad = cfg.evaluate_post("this response breaks the rules")
        assert bad.context["content_checks_slm_failed"] is True
        assert _fake_backend["rubric"].startswith("Does the response")

    def test_backend_error_is_failclosed(self) -> None:
        cfg = rj.JudgeConfig()
        cfg.load_from_bundle(self._bundle(model="boom-model"))
        result = cfg.evaluate_post("safe")
        # a raising backend -> errors (caller denies), NOT a silent pass
        assert result.errors and "SLM judge raised" in result.errors[0]
        assert result.context == {}

    def test_unknown_backend_is_failclosed(self) -> None:
        cfg = rj.JudgeConfig()
        cfg.load_from_bundle(self._bundle(backend="nope"))
        result = cfg.evaluate_post("safe")
        assert result.errors and "unknown SLM-judge backend" in result.errors[0]


def test_slm_backend_refuses_to_guess_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # NOTHING configured -- no judge env AND no agent model -> raise (fail
    # closed), never silently hit some default hosted API.
    for var in (rj._ENV_MODEL, rj._ENV_BASE_URL, "OPENAI_CHAT_COMPLETION_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="needs a model"):
        rj._slm_judge("resp", "rule", 0.5, {})


def test_defaults_to_the_agents_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # no dedicated judge endpoint, but the agent has a model configured (the
    # OPENAI_* env the MAF client uses) -> the judge reuses it, no extra infra.
    for var in (rj._ENV_MODEL, rj._ENV_BASE_URL, rj._ENV_KEY):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://agent-model:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-agent")
    model, base_url, key = rj._resolve_endpoint({})
    assert model == "gpt-4o-mini" and base_url == "http://agent-model:8000/v1" and key == "sk-agent"

    # a dedicated judge endpoint overrides the agent's model when set
    monkeypatch.setenv(rj._ENV_MODEL, "qwen2.5:1.5b")
    monkeypatch.setenv(rj._ENV_BASE_URL, "http://judge-sidecar:11434/v1")
    model, base_url, _ = rj._resolve_endpoint({})
    assert model == "qwen2.5:1.5b" and base_url == "http://judge-sidecar:11434/v1"


class TestJudgeEnforcement:
    """End to end through real SDK code: the SLM judge verdict, merged into the
    post-stage context, is enforced by a real PolicyEngine's @stage("post")
    forbid -- a FAIL denies, a PASS allows, a judge error fails closed. The
    judge backend is faked so no endpoint is needed; everything else is real."""

    @pytest.fixture(autouse=True)
    def _fake_backend(self):  # type: ignore[no-untyped-def]
        def fake(response, rubric, threshold, options):  # noqa: ANN001, ANN202
            if "boom" in rubric:
                raise RuntimeError("endpoint down")
            return rj.JudgeResult("safe" in response, 1.0 if "safe" in response else 0.0, "fake")

        rj.JUDGES["fake"] = fake
        yield
        rj.JUDGES.pop("fake", None)

    @staticmethod
    def _policy_dir(tmp_path, rubric="Stay on topic."):  # type: ignore[no-untyped-def]
        import json as _json

        d = tmp_path / "pol"
        d.mkdir()
        (d / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);\n'
        )
        # exactly what control-plane's _forbid_slm_verdict renders
        (d / "45-tier2.cedar").write_text(
            '@id("content-check-slm-judge")\n'
            '@stage("post")\n'
            'forbid (principal, action == Action::"model_call", resource)\n'
            "when {\n  context has content_checks_slm_failed &&\n"
            "  context.content_checks_slm_failed == true\n};\n"
        )
        (d / "response_judge.json").write_text(
            _json.dumps(
                [{"library_id": "j", "context_key": "content_checks_slm_failed",
                  "backend": "fake", "rubric": rubric, "threshold": 0.5}]
            )
        )
        return d

    def _mw(self, tmp_path, rubric="Stay on topic."):  # type: ignore[no-untyped-def]
        from parapetai_agent.identity import Caller
        from parapetai_agent.maf import ParapetChatMiddleware
        from parapetai_agent.policy.engine import PolicyEngine

        d = self._policy_dir(tmp_path, rubric)
        cfg = rj.JudgeConfig()
        cfg.load_from_bundle({"response_judge.json": (d / "response_judge.json").read_text()})
        engine = PolicyEngine(d)
        return ParapetChatMiddleware(engine, Caller(agent_id="j-test", tenant="default"), judge=cfg)

    @staticmethod
    def _ctx(answer):  # type: ignore[no-untyped-def]
        from agent_framework import ChatContext, ChatResponse, Message
        from agent_framework.openai import OpenAIChatCompletionClient

        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hi"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", [answer])])

        return ctx, call_next

    async def test_passing_response_is_allowed(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        mw = self._mw(tmp_path)
        ctx, call_next = self._ctx("this is a safe answer")
        await mw.process(ctx, call_next)  # no raise -> allowed

    async def test_failing_response_is_denied_post_stage(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from parapetai_agent.maf import GovernanceDenied

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        mw = self._mw(tmp_path)
        ctx, call_next = self._ctx("this response violates the rule")
        with pytest.raises(GovernanceDenied) as exc:
            await mw.process(ctx, call_next)
        assert exc.value.decision.effect == "deny" and not exc.value.decision.allowed

    async def test_judge_error_fails_closed(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from parapetai_agent.maf import GovernanceDenied

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        mw = self._mw(tmp_path, rubric="boom")  # fake backend raises on this rubric
        ctx, call_next = self._ctx("this is a safe answer")
        with pytest.raises(GovernanceDenied):  # error before Cedar -> hard deny
            await mw.process(ctx, call_next)
