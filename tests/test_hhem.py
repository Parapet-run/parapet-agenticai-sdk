"""HHEM groundedness backend (parapetai_agent/_hhem.py).

The real model is multi-hundred-MB weights, so these tests inject a fake
predictor (the documented set_predictor seam) and assert the wiring: a pair is
routed through as (premise, hypothesis), the score is returned in [0, 1], and a
missing extra surfaces as an actionable ImportError rather than a silent pass.
That's exactly the contract groundedness.py's 'hhem' backend depends on.
"""

from __future__ import annotations

import pytest

from parapetai_agent import _hhem


@pytest.fixture(autouse=True)
def _reset_predictor() -> None:
    _hhem.set_predictor(None)
    yield
    _hhem.set_predictor(None)


def test_injected_predictor_receives_premise_hypothesis_pair() -> None:
    seen: list[tuple[str, str]] = []

    def fake(pairs: list[tuple[str, str]]) -> list[float]:
        seen.extend(pairs)
        return [0.91 for _ in pairs]

    _hhem.set_predictor(fake)
    score = _hhem.hhem_consistency_score(
        premise="The refund cap is $500.", hypothesis="Refunds are capped at $500."
    )
    assert score == pytest.approx(0.91)
    # premise is the source/grounding, hypothesis is the model's response
    assert seen == [("The refund cap is $500.", "Refunds are capped at $500.")]


def test_low_consistency_score_flows_through() -> None:
    _hhem.set_predictor(lambda pairs: [0.03])
    assert _hhem.hhem_consistency_score(premise="Sky is blue.", hypothesis="Sky is green.") < 0.1


def test_missing_extra_raises_actionable_importerror(monkeypatch: pytest.MonkeyPatch) -> None:
    # No predictor injected + transformers import forced to fail -> the loader
    # must raise an ImportError that tells the operator how to enable it.
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *a: object, **k: object) -> object:
        if name == "transformers" or name.startswith("transformers."):
            raise ImportError("no transformers")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match=r"pip install"):
        _hhem.hhem_consistency_score(premise="a", hypothesis="b")
