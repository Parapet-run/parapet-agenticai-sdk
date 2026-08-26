"""The provider-agnostic `litellm` judge backend (response_judge.JUDGES).

Why this backend exists: `slm` builds an OpenAI/AzureOpenAI client directly, so
it can only reach OpenAI-wire endpoints -- the one provider-specific corner of
an SDK whose premise is provider neutrality. These tests pin the properties
that make the litellm backend a replacement for N hand-rolled vendor clients
rather than an N+1th one:

  * it reuses the EXISTING config surface (options + PARAPET_SLM_JUDGE_*), so
    selecting it needs no control-plane change;
  * it sends the SAME prompt as `slm`, so a control-plane eval and the runtime
    judge cannot silently disagree;
  * it fails CLOSED on every error path, like every other judge;
  * and it labels its verdicts `litellm`, so the audit trail says which judge
    actually ran.

Everything here is offline except the final live test, which is skipped unless
a real ANTHROPIC_API_KEY is present.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

from parapetai_agent import response_judge as rj

RUBRIC = "The response must not promise a refund."
RESPONSE = "I cannot promise a refund."


def _fake_litellm(monkeypatch: pytest.MonkeyPatch, content: str, *, boom: bool = False) -> dict:
    """Install a stand-in `litellm` module and return the dict its completion()
    call is recorded into. The backend imports litellm INSIDE the function, so
    replacing sys.modules is enough -- no import-time dependency."""
    seen: dict[str, Any] = {}

    def completion(**kwargs: Any) -> Any:
        seen.update(kwargs)
        if boom:
            raise RuntimeError("endpoint down")
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    module = types.ModuleType("litellm")
    module.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", module)
    return seen


@pytest.fixture(autouse=True)
def _clear_judge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer machine may export any of these; without clearing them the
    config-precedence tests below would pass or fail by accident."""
    for var in (rj._ENV_MODEL, rj._ENV_BASE_URL, rj._ENV_KEY):
        monkeypatch.delenv(var, raising=False)


# ── registry ─────────────────────────────────────────────────────────


def test_both_backends_are_registered() -> None:
    assert set(rj.JUDGES) == {"slm", "litellm"}


def test_judge_response_routes_to_the_litellm_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_litellm(monkeypatch, '{"pass": true, "score": 0.9}')

    result = rj.judge_response(
        RESPONSE, RUBRIC, backend="litellm", options={"model": "anthropic/claude-haiku-4-5"}
    )

    assert result.passed is True
    assert result.backend == "litellm"


# ── config surface: the existing one, not a parallel one ─────────────


def test_model_comes_from_the_config_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`options` is what the control plane renders into response_judge.json --
    reusing it is what makes this backend selectable with no control-plane
    change."""
    seen = _fake_litellm(monkeypatch, "PASS")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert seen["model"] == "anthropic/claude-haiku-4-5"


def test_model_falls_back_to_the_existing_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_litellm(monkeypatch, "PASS")
    monkeypatch.setenv(rj._ENV_MODEL, "groq/llama-3.3-70b-versatile")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {})

    assert seen["model"] == "groq/llama-3.3-70b-versatile"


def test_config_entry_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_litellm(monkeypatch, "PASS")
    monkeypatch.setenv(rj._ENV_MODEL, "groq/from-env")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/from-options"})

    assert seen["model"] == "anthropic/from-options"


def test_unset_endpoint_fields_are_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing api_base=None makes litellm skip its own provider-default
    resolution, which breaks every hosted provider -- the whole point of
    delegating routing to it. Omit rather than pass None."""
    seen = _fake_litellm(monkeypatch, "PASS")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert "api_base" not in seen
    assert "api_key" not in seen


def test_pinned_endpoint_and_key_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted judge (Ollama, vLLM) needs both; a hosted one needs
    neither."""
    seen = _fake_litellm(monkeypatch, "PASS")

    rj._litellm_judge(
        RESPONSE,
        RUBRIC,
        0.5,
        {"model": "ollama/qwen2.5", "base_url": "http://judge:11434", "key": "k"},
    )

    assert seen["api_base"] == "http://judge:11434"
    assert seen["api_key"] == "k"


def test_output_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verdict is one small JSON object; an unbounded judge call is a budget
    leak on every governed response."""
    seen = _fake_litellm(monkeypatch, "PASS")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert seen["max_tokens"] == rj._LITELLM_MAX_TOKENS


# ── prompt parity with the slm backend ───────────────────────────────


def test_sends_the_same_prompt_as_the_slm_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control plane scores evals through this same module
    (parapetai_control.scorers.judge_response). If the two backends' prompts
    drifted apart, an eval could pass while the runtime judge failed the very
    same response -- a silent divergence between what you tested and what you
    enforce."""
    seen = _fake_litellm(monkeypatch, "PASS")

    rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert seen["messages"] == rj._judge_messages(RUBRIC, RESPONSE)
    assert seen["messages"][0]["content"] == rj._RUBRIC_SYSTEM


# ── verdict labelling ────────────────────────────────────────────────


def test_verdicts_are_labelled_litellm_not_slm(monkeypatch: pytest.MonkeyPatch) -> None:
    """_parse_verdict used to hard-code "slm". A verdict labelled with the
    wrong backend is a lie in the audit trail -- it is how an operator tells
    which judge actually produced a block."""
    _fake_litellm(monkeypatch, '{"pass": false, "score": 0.1, "reason": "promised refund"}')

    result = rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert result.backend == "litellm"
    assert result.passed is False


def test_slm_backend_still_labels_slm() -> None:
    """Regression guard on the shared parser's default -- threading `backend`
    through must not have relabelled the pre-existing backend."""
    assert rj._parse_verdict('{"pass": true}', 0.5).backend == "slm"


# ── fail closed ──────────────────────────────────────────────────────


def test_missing_model_raises_rather_than_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same contract as _build_judge_client's "refusing to guess": a judge that
    silently picked some default endpoint would send response text somewhere
    the operator never authorised."""
    _fake_litellm(monkeypatch, "PASS")

    with pytest.raises(RuntimeError, match="needs a model"):
        rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {})


def test_transport_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """maf.py turns a raised judge error into a hard deny BEFORE Cedar runs, so
    a missing verdict can never read as "passed". Swallowing this would fail
    open."""
    _fake_litellm(monkeypatch, "", boom=True)

    with pytest.raises(RuntimeError, match="endpoint down"):
        rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})


def test_missing_litellm_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """litellm is an extra, so this is the error an adopter who selected the
    backend without installing it will actually hit. It has to say how to fix
    it."""
    monkeypatch.setitem(sys.modules, "litellm", None)  # forces ImportError on `import litellm`

    with pytest.raises(ImportError, match=r"parapetai-agent\[judge\]"):
        rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})


def test_unparseable_verdict_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """fail-toward-flagging: an ambiguous judge answer is a possible violation,
    never a pass."""
    _fake_litellm(monkeypatch, "I'm not sure how to answer that.")

    result = rj._litellm_judge(RESPONSE, RUBRIC, 0.5, {"model": "anthropic/claude-haiku-4-5"})

    assert result.passed is False
    assert result.backend == "litellm"


# ── live: a real provider, behind a real key ─────────────────────────

_LIVE_MODEL = os.environ.get("PARAPET_LIVE_JUDGE_MODEL", "anthropic/claude-haiku-4-5")

live = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live judge test needs a real ANTHROPIC_API_KEY (set it, or run the offline tests only)",
)


@live
@pytest.mark.parametrize(
    "response,expected_pass",
    [
        ("Your order shipped on Tuesday and arrives Friday.", True),
        ("Absolutely, I promise you a full refund right now.", False),
    ],
)
def test_live_anthropic_judge_agrees_with_the_rubric(response: str, expected_pass: bool) -> None:
    """End-to-end against a real provider, proving the backend actually routes
    and that the verdict is usable -- the offline tests above all run against a
    stand-in and would pass even if litellm's real call signature changed.

    Asserts only the boolean, on two cases chosen to be unambiguous FOR A SMALL
    JUDGE. That qualifier is load-bearing: the passing case originally read "I
    cannot promise a refund for this order", which a human reads as obviously
    compliant -- and the live judge failed it, with the reason "Response
    mentions refund, violating the rule against promising refunds". A
    small model over-triggers on the mere presence of the rubric's keyword, so
    the compliant fixture here avoids the word entirely. That is a real
    property of small-model judging, not a flake to retry away; it is also why
    the docs recommend a decorrelated judge model rather than reusing the
    agent's own.

    Model output is not deterministic, so pinning the score or the reason text
    would buy nothing and produce a flaky test. The pass/fail call is the only
    part enforcement depends on.
    """
    result = rj.judge_response(response, RUBRIC, backend="litellm", options={"model": _LIVE_MODEL})

    assert result.passed is expected_pass
    assert result.backend == "litellm"
    assert 0.0 <= result.score <= 1.0


@live
def test_live_backend_reaches_a_non_openai_wire_provider() -> None:
    """The point of the whole backend: Anthropic is not an OpenAI-compatible
    endpoint, so the `slm` backend cannot reach it at all. If this passes,
    provider-agnostic judging is real rather than aspirational."""
    assert _LIVE_MODEL.split("/", 1)[0] not in {"openai", "azure"}

    result = rj.judge_response(
        "Your order shipped on Tuesday.",
        RUBRIC,
        backend="litellm",
        options={"model": _LIVE_MODEL},
    )

    assert result.backend == "litellm"
