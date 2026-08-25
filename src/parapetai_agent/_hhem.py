"""Optional HHEM-2.1-Open groundedness backend (parapetai_agent.groundedness's
``hhem`` backend).

Vectara's HHEM-2.1-Open (Apache-2.0) is a small local cross-encoder that scores
the *factual consistency* of a hypothesis against a premise -- exactly the
groundedness question ("is this response supported by the retrieved sources?"),
but with a trained model instead of the dependency-free lexical heuristic. It
runs fully locally, so the response text never leaves the process: Parapet's
content-free posture is preserved even though this backend, unlike lexical,
looks at the text.

This module is intentionally tiny and import-light. ``transformers``/``torch``
are heavy and are NOT dependencies of this package (torch would drag the whole
CUDA wheel stack into the lock) -- enable the backend explicitly with
``pip install transformers torch`` (see docs/GROUNDEDNESS_HHEM.md). They are
imported lazily the first time a score is requested, and groundedness.py
imports ``hhem_consistency_score`` lazily too, so a bundle that never selects
the ``hhem`` backend pays nothing.

The model is loaded once and cached. ``set_predictor`` lets a caller inject a
predictor -- either a preloaded model shared across processes, or a fake in a
test -- so the wiring is verifiable without pulling multi-hundred-MB weights.
"""

from __future__ import annotations

import os
from collections.abc import Callable

# (premise, hypothesis) pairs -> one consistency probability in [0, 1] each.
Predictor = Callable[[list[tuple[str, str]]], list[float]]

# vectara/hallucination_evaluation_model on the HF hub; override for an
# air-gapped mirror or a pinned local snapshot via the env var.
MODEL_ID = os.environ.get("PARAPET_HHEM_MODEL", "vectara/hallucination_evaluation_model")

_predictor: Predictor | None = None


def set_predictor(fn: Predictor | None) -> None:
    """Inject (or clear, with ``None``) the predictor used for scoring. Use to
    share a single preloaded model, or to substitute a fake in tests so the
    backend is exercised without downloading weights."""
    global _predictor
    _predictor = fn


def _load_predictor() -> Predictor:
    """Build the real HHEM predictor. Raises ImportError with an actionable
    message when the optional extra isn't installed -- groundedness.py's backend
    selection turns that into a fail-closed 'backend unavailable' error rather
    than a silent pass."""
    try:
        from transformers import AutoModelForSequenceClassification
    except ImportError as exc:  # pragma: no cover -- exercised via a forced fake
        raise ImportError(
            "the HHEM groundedness backend needs transformers + torch: "
            "pip install transformers torch (see docs/GROUNDEDNESS_HHEM.md)"
        ) from exc

    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_ID, trust_remote_code=True
    )

    def predict(pairs: list[tuple[str, str]]) -> list[float]:
        # HHEM-2.1-Open exposes .predict(list[(premise, hypothesis)]) -> a
        # tensor of consistency probabilities. Kept behind this adapter so the
        # rest of the code depends only on the plain list[float] contract.
        scores = model.predict(pairs)
        return [float(s) for s in scores]

    return predict


def _remote_predictor(url: str) -> Predictor:
    """Predictor that calls a standalone HHEM eval service instead of loading
    the model in-process -- the 'plug it out into a separate service' path.
    Set PARAPET_HHEM_URL to the service base URL (see services/hhem-eval/).

    Same content-free contract as the in-process backend: only (premise,
    hypothesis) pairs cross to a service inside your own network, and only a
    score comes back -- no text ever leaves your environment. The SDK stays
    light (no torch), and the eval scales independently of the agent."""
    import json as _json
    from urllib import request as _request

    def predict(pairs: list[tuple[str, str]]) -> list[float]:
        body = _json.dumps({"pairs": [[p, h] for (p, h) in pairs]}).encode()
        req = _request.Request(  # noqa: S310 -- operator-set in-VPC URL
            f"{url}/score", data=body, headers={"Content-Type": "application/json"}
        )
        with _request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- operator-set in-VPC URL
            data = _json.loads(resp.read())
        return [float(s) for s in data["scores"]]

    return predict


def _get_predictor() -> Predictor:
    global _predictor
    if _predictor is None:
        # Deployment-agnostic: same "hhem" backend, in-process by default
        # (loads torch + the model here), or a standalone eval service when
        # PARAPET_HHEM_URL is set -- flip between the two with one env var, no
        # policy/backend/config change.
        remote = os.environ.get("PARAPET_HHEM_URL", "").rstrip("/")
        _predictor = _remote_predictor(remote) if remote else _load_predictor()
    return _predictor


def hhem_consistency_score(*, premise: str, hypothesis: str) -> float:
    """Factual-consistency probability of ``hypothesis`` given ``premise``, in
    [0, 1] -- higher means better supported (more grounded). One pair per call;
    groundedness.py maps this onto its threshold."""
    return float(_get_predictor()([(premise, hypothesis)])[0])
