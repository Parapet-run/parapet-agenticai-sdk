"""Runtime groundedness / hallucination check (QUAL-1).

Answers, for a model response and the source text the model was given (its
prompt + any retrieved/tool context): **is this answer supported by the
source, or did the model make it up?** -- the market's #1 pain, and the piece
that completes Parapet's content guardrail (today it blocks PII/secrets/prompt
injection on the way IN; this checks faithfulness on the way OUT).

Two things make this fit Parapet specifically:

  * **In-process, content-private.** The check runs where the response already
    lives (the post-stage hook), so the answer text never leaves the box. Only
    a content-free verdict (a groundedness bucket / a boolean) flows to the
    control plane -- the same posture as every other decision.
  * **Tiered, like the content checks.** The default backend is dependency-free
    lexical NLI: split the answer into claims, check each for support in the
    source, and flag numeric contradictions. It is a proxy -- honest about that
    -- and needs no model download, so it runs everywhere. A heavier,
    higher-accuracy backend (Vectara HHEM-2.1-Open, Apache-2.0, a small local
    cross-encoder) plugs in behind an optional extra via the BACKENDS registry
    without changing this module's interface, exactly the way
    parapetai_agent.content_checks left room for a heavier scanner.

Fail-closed is the caller's job (maf.py), mirroring the tier-2 contract: a
backend that raises yields errors, and the caller denies rather than letting a
missing verdict read as "grounded".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Small, high-frequency function words. Dropping these focuses "support" on
# content words so "The capital is Paris" vs source "Paris is the capital"
# reads as supported, not penalised for word order / filler.
_STOPWORDS = frozenset(
    "a an the of to in on at for and or but is are was were be been being it its this that these "
    "those as by with from into i you he she they we me my your our their his her not no do does "
    "did have has had will would can could should may might must here there what which who whom "
    "how when where why then than so if about over under out up down".split()
)

_WORD = re.compile(r"[A-Za-z0-9]+")
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _content_tokens(text: str) -> set[str]:
    return {w for w in (m.group().lower() for m in _WORD.finditer(text)) if w not in _STOPWORDS}


def _numbers(text: str) -> set[str]:
    # Normalise "1,000" / "1000.0" -> "1000" so formatting doesn't count as a
    # contradiction, while a genuinely different figure still does.
    out = set()
    for m in _NUM.finditer(text):
        raw = m.group().replace(",", "")
        try:
            out.add(str(int(float(raw))) if float(raw).is_integer() else str(float(raw)))
        except ValueError:  # pragma: no cover -- regex only yields numeric text
            continue
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text.strip()) if s.strip()]


@dataclass
class GroundednessResult:
    """score in [0,1] (fraction of the answer's claims supported by the
    source), grounded = score >= threshold and no hard numeric contradiction.
    unsupported_claims/reason are for operator display, never shipped as
    content to the control plane."""

    score: float
    grounded: bool
    backend: str
    threshold: float
    unsupported_claims: list[str] = field(default_factory=list)
    contradiction: bool = False
    reason: str = ""


def _lexical_groundedness(response: str, sources: str, threshold: float) -> GroundednessResult:
    claims = _sentences(response)
    if not claims:
        # Nothing asserted -> nothing to hallucinate. Grounded by definition.
        return GroundednessResult(1.0, True, "lexical", threshold, reason="empty response")
    src_tokens = _content_tokens(sources)
    src_numbers = _numbers(sources)
    supported = 0
    unsupported: list[str] = []
    contradiction = False
    for claim in claims:
        c_tokens = _content_tokens(claim)
        if not c_tokens:
            supported += 1  # a claim with only filler words asserts nothing checkable
            continue
        overlap = len(c_tokens & src_tokens) / len(c_tokens)
        # A number in the claim that appears nowhere in the source is a strong
        # fabrication signal (wrong price, wrong date), independent of overlap.
        claim_numbers = _numbers(claim)
        novel_numbers = claim_numbers - src_numbers
        if novel_numbers:
            contradiction = True
            unsupported.append(claim)
            continue
        if overlap >= 0.6:
            supported += 1
        else:
            unsupported.append(claim)
    score = supported / len(claims)
    grounded = score >= threshold and not contradiction
    reason = (
        "supported by source"
        if grounded
        else (
            "numeric claim absent from source"
            if contradiction
            else f"{len(unsupported)}/{len(claims)} claims unsupported"
        )
    )
    return GroundednessResult(
        round(score, 3), grounded, "lexical", threshold, unsupported, contradiction, reason
    )


def _hhem_groundedness(response: str, sources: str, threshold: float) -> GroundednessResult:
    """Vectara HHEM-2.1-Open backend (optional extra). Higher-accuracy factual
    consistency from a small local cross-encoder; still fully local / content-
    private. Raises ImportError if the extra isn't installed -- the caller's
    backend selection handles that, so import stays lazy here."""
    from parapetai_agent._hhem import hhem_consistency_score

    score = float(hhem_consistency_score(premise=sources, hypothesis=response))
    return GroundednessResult(
        round(score, 3),
        score >= threshold,
        "hhem",
        threshold,
        reason="HHEM factual-consistency score",
    )


# scanner_id -> backend. 'lexical' is always present (no deps); 'hhem' is
# available only when its optional extra is installed. Same additive-registry
# shape as parapetai_agent.content_checks.SCANNERS.
BACKENDS: dict[str, Callable[[str, str, float], GroundednessResult]] = {
    "lexical": _lexical_groundedness,
    "hhem": _hhem_groundedness,
}

DEFAULT_THRESHOLD = 0.6


def score_groundedness(
    response: str,
    sources: str,
    *,
    backend: str = "lexical",
    threshold: float = DEFAULT_THRESHOLD,
) -> GroundednessResult:
    """Score how well `response` is supported by `sources`. Unknown backend
    falls back to lexical rather than raising, so a bundle that names a backend
    this SDK build doesn't ship still checks groundedness (degraded, not off) --
    a fail-toward-checking default; hard failures are the backend's own to
    raise, and the caller denies on those."""
    fn = BACKENDS.get(backend, _lexical_groundedness)
    return fn(response, sources, threshold)


# ── bundle-configured post-stage check (wired into maf.py) ───────────────

CONFIG_FILENAME = "groundedness.json"


@dataclass
class _GroundednessEntry:
    library_id: str
    context_key: str
    backend: str
    threshold: float


@dataclass
class GroundednessEvalResult:
    """context: flat top-level keys the post-stage Cedar rule gates on, each
    set to True iff that check found the response UNGROUNDED (a real Cedar
    bool, not a string). errors: non-empty means a configured backend could
    not run -- the caller (maf.py) turns that into a hard deny BEFORE Cedar
    runs, the same fail-closed contract the tier-2 scanners use, so a missing
    verdict can never read as 'grounded'."""

    context: dict[str, bool]
    errors: tuple[str, ...]


class GroundednessConfig:
    """Loads the control plane's groundedness.json (rendered by
    parapetai_control.content_checks.render_groundedness_json) and runs the
    selected checks against a model response at the post stage. Empty until a
    bundle actually selects a groundedness check -- evaluate_post() is then a
    harmless no-op returning no context and no errors."""

    def __init__(self) -> None:
        self._entries: list[_GroundednessEntry] = []

    @property
    def active(self) -> bool:
        return bool(self._entries)

    def load_from_bundle(self, files: dict[str, str]) -> None:
        raw = files.get(CONFIG_FILENAME)
        if not raw:
            self._entries = []
            return
        try:
            data: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._entries = []
            return
        entries: list[_GroundednessEntry] = []
        for e in data if isinstance(data, list) else []:
            try:
                entries.append(
                    _GroundednessEntry(
                        library_id=str(e["library_id"]),
                        context_key=str(e["context_key"]),
                        backend=str(e.get("backend", "lexical")),
                        threshold=float(e.get("threshold", DEFAULT_THRESHOLD)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._entries = entries

    def evaluate_post(self, response: str, sources: str) -> GroundednessEvalResult:
        """Score `response` against `sources` for every selected check. A
        backend that raises is captured as an error (caller denies); otherwise
        the check's context_key is set to True iff the answer was ungrounded."""
        context: dict[str, bool] = {}
        errors: list[str] = []
        for entry in self._entries:
            try:
                result = score_groundedness(
                    response, sources, backend=entry.backend, threshold=entry.threshold
                )
            except Exception as exc:  # noqa: BLE001 -- fail closed: any error -> caller denies
                errors.append(f"{entry.library_id}: groundedness backend raised: {exc}")
                continue
            context[entry.context_key] = not result.grounded
        return GroundednessEvalResult(context, tuple(errors))
