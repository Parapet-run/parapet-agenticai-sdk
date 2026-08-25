"""SLM-judge response evaluation (runtime eval + guardrail, post-stage).

Where groundedness (parapetai_agent.groundedness) answers "is this response
supported by its sources?", the SLM judge answers an operator-defined *rubric*
question about a response -- "does this violate the refund policy?", "is this
off-topic?", "is the tone acceptable?" -- using a SMALL language model (an SLM
judge), not a frontier LLM.

Backends plug into the JUDGES registry, exactly like groundedness.BACKENDS:

  * ``slm`` -- an instruct model behind an OpenAI-compatible endpoint. By
    default it REUSES the model the agent is already configured with (the
    OPENAI_* env the MAF client uses) -- the agent already has an LLM, so the
    judge needs no extra infrastructure to start. Point PARAPET_SLM_JUDGE_* at a
    dedicated SMALL model (Ollama / vLLM) when you want cheaper, decorrelated
    judging (a model is a weak judge of its own output). Rubric and endpoint are
    config-driven (from the signed bundle, with env fallbacks), so the same SDK
    build serves any judge without a code change.

This module only *produces* a verdict. The verdict becomes an *enforced*
decision through Cedar: the control plane renders a ``@stage("post") forbid``
gated on the context key set here, so a FAIL is denied in-process and the
decision is signed, versioned, and regression-testable -- governance OF the
judge, not a standalone probabilistic gatekeeper.

Content-free contract holds end to end: the response text is read only here,
in-process, and (for the ``slm`` backend) sent only to the operator's OWN
endpoint -- never to the control plane. Only the pass/fail verdict leaves as
telemetry.

Fail-closed: a backend that errors is captured as an error the caller (maf.py)
turns into a hard deny BEFORE Cedar runs -- the same contract the tier-2
scanners and groundedness use, so a missing verdict can never read as "passed".
An ambiguous model verdict likewise defaults to FAIL (fail-toward-flagging).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD = 0.5
# Env fallbacks used only when a config entry doesn't pin them. The endpoint is
# the operator's own small-model server; nothing here defaults to a hosted API.
_ENV_MODEL = "PARAPET_SLM_JUDGE_MODEL"
_ENV_BASE_URL = "PARAPET_SLM_JUDGE_URL"
_ENV_KEY = "PARAPET_SLM_JUDGE_KEY"

_YES = re.compile(r"\b(pass|passed|yes|true|ok|acceptable|allow|compliant)\b", re.I)
_NO = re.compile(r"\b(fail|failed|no|false|violat\w*|reject|deny|unacceptable)\b", re.I)


@dataclass
class JudgeResult:
    """passed = the response satisfies the rubric. score in [0,1] is the
    model's confidence when it gives one (else 1.0/0.0 from the verdict).
    reason is for operator display only, never shipped as content."""

    passed: bool
    score: float
    backend: str
    reason: str = ""


def _parse_verdict(text: str, threshold: float) -> JudgeResult:
    """Parse a small model's answer into pass/fail. Accepts a JSON object
    ({"pass": true, "score": 0.9}), or free text containing PASS/FAIL-like
    tokens. Anything unrecognised -> FAIL (fail-toward-flagging), so an
    ambiguous judge output is treated as a possible violation, not a pass."""
    raw = (text or "").strip()
    # 1) structured JSON verdict -- accept it even when the model wraps the
    # JSON in markdown fences or surrounding prose (Claude and others often
    # answer ```json {...} ``` or "Here is my verdict: {...}"). Try the raw
    # text first, then the first {...} object substring found in it. Without
    # this, a fenced verdict falls through to the free-text branch below and a
    # perfectly good {"pass": true, "reason": "...no promises..."} is read as a
    # FAIL because the reason text happens to contain a NO-token.
    _candidates = [raw]
    _m = re.search(r"\{.*\}", raw, re.S)
    if _m:
        _candidates.append(_m.group(0))
    for _cand in _candidates:
        try:
            obj = json.loads(_cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and ("pass" in obj or "score" in obj):
            score = float(obj.get("score", 1.0 if obj.get("pass") else 0.0))
            passed = bool(obj["pass"]) if "pass" in obj else score >= threshold
            return JudgeResult(passed, round(score, 3), "slm", str(obj.get("reason", "")))
    # 2) free-text verdict: a NO token anywhere means fail; else a YES token passes
    if _NO.search(raw):
        return JudgeResult(False, 0.0, "slm", "verdict: fail")
    if _YES.search(raw):
        return JudgeResult(True, 1.0, "slm", "verdict: pass")
    return JudgeResult(False, 0.0, "slm", "unparseable judge verdict")


_RUBRIC_SYSTEM = (
    "You are a strict evaluation judge. Decide whether the ASSISTANT RESPONSE "
    "satisfies the RULE. Answer with a JSON object only: "
    '{"pass": true|false, "score": 0.0-1.0, "reason": "<=12 words"}. '
    "pass=true means the response satisfies the rule."
)


def _build_judge_client(options: dict[str, Any]) -> tuple[Any, str]:
    """Resolve (client, model) for the judge, in priority order:

      1. the config entry (per-agent pin, from the bundle) or a DEDICATED judge
         endpoint (PARAPET_SLM_JUDGE_* env) -- recommended: a cheap, decorrelated
         small model. With its own OpenAI-compatible URL it uses a plain client;
         with only a model name set and the agent on Azure, that name is treated
         as a deployment on the agent's Azure endpoint.
      2. the agent's own OpenAI-style model (OPENAI_CHAT_COMPLETION_MODEL) -- a
         plain OpenAI client; base_url optional (hosted OpenAI default).
      3. the agent's own AZURE config (AZURE_OPENAI_ENDPOINT / _API_KEY /
         _API_VERSION / _CHAT_COMPLETION_MODEL) -- an AzureOpenAI client.

    Steps 2-3 are the 'reuse what the agent already has' default: the agent
    already has an LLM configured, so the judge borrows it with no extra
    infrastructure. It never guesses a hosted API -- only the operator's own
    already-configured provider. Azure is first-class here because the MAF
    OpenAIChatCompletionClient this repo runs is itself Azure-configured
    (AZURE_OPENAI_*); without step 3 the 'reuse the agent's model' promise
    silently fails on every Azure deployment and the judge fail-closes on ALL
    traffic (found live). Model/flavor are resolved from env FIRST so a genuine
    'no model configured' still raises the clean RuntimeError below even when the
    openai client isn't installed -- the caller fails closed on either."""
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    _azure_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

    dedicated_model = options.get("model") or os.environ.get(_ENV_MODEL)
    dedicated_url = options.get("base_url") or os.environ.get(_ENV_BASE_URL)
    dedicated_key = options.get("key") or os.environ.get(_ENV_KEY)

    flavor: str | None = None
    model: str | None = None
    kwargs: dict[str, Any] = {}

    if dedicated_model:
        model = dedicated_model
        if dedicated_url or not azure_endpoint:
            flavor = "openai"
            kwargs = {
                "base_url": dedicated_url,
                "api_key": dedicated_key or os.environ.get("OPENAI_API_KEY", "local"),
            }
        else:
            # dedicated model name, no dedicated URL, agent is on Azure -> run
            # the dedicated deployment on the agent's own Azure endpoint.
            azure_key = dedicated_key or os.environ.get("AZURE_OPENAI_API_KEY")
            if azure_key:
                flavor = "azure"
                kwargs = {
                    "azure_endpoint": azure_endpoint,
                    "api_key": azure_key,
                    "api_version": _azure_version,
                }
    else:
        openai_model = os.environ.get("OPENAI_CHAT_COMPLETION_MODEL")
        if openai_model:
            flavor, model = "openai", openai_model
            kwargs = {
                "base_url": os.environ.get("OPENAI_BASE_URL"),
                "api_key": os.environ.get("OPENAI_API_KEY", "local"),
            }
        elif azure_endpoint:
            azure_model = os.environ.get("AZURE_OPENAI_CHAT_COMPLETION_MODEL")
            azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
            if azure_model and azure_key:
                flavor, model = "azure", azure_model
                kwargs = {
                    "azure_endpoint": azure_endpoint,
                    "api_key": azure_key,
                    "api_version": _azure_version,
                }

    if flavor is None or not model:
        raise RuntimeError(
            "SLM judge needs a model: set PARAPET_SLM_JUDGE_MODEL for a dedicated "
            "judge, or rely on the agent's OPENAI_CHAT_COMPLETION_MODEL / "
            "AZURE_OPENAI_* config; refusing to guess"
        )

    try:
        from openai import AzureOpenAI, OpenAI
    except ImportError as exc:  # pragma: no cover -- exercised via a forced fake
        raise ImportError(
            "the SLM judge backend needs the 'openai' client: pip install openai"
        ) from exc

    client = AzureOpenAI(**kwargs) if flavor == "azure" else OpenAI(**kwargs)
    return client, model


# Model families disagree on how to cap output and whether temperature may be
# set. Reasoning models (gpt-5-*) REQUIRE max_completion_tokens, reject
# max_tokens, and reject any temperature != 1; classic chat models take
# max_tokens (+ temperature 0 for a deterministic verdict); a few endpoints
# (some Mistral deployments) reject max_completion_tokens outright. The judge
# reuses whatever model the agent runs, so it cannot assume one shape -- it
# tries the reasoning shape first (the common default here), then the classic
# shape, then bare provider defaults, and fails closed only if none work. The
# first shape the configured model accepts wins on the first call; the others
# cost an extra call only when a parameter shape is rejected. Budget is generous
# on the reasoning shape because that model spends hidden reasoning tokens before
# the tiny JSON verdict -- too small a cap returns empty text, which
# _parse_verdict reads as FAIL, silently blocking every response.
_JUDGE_PARAM_SHAPES: tuple[dict[str, Any], ...] = (
    {"max_completion_tokens": 2000},
    {"max_tokens": 256, "temperature": 0},
    {},
)


def _judge_completion(client: Any, model: str, messages: list[dict[str, str]]) -> Any:
    """Call the judge model, tolerating per-family parameter quirks (see
    _JUDGE_PARAM_SHAPES). Returns the first successful completion; re-raises the
    last error if every shape fails, so the caller fails closed."""
    last_exc: Exception | None = None
    for extra in _JUDGE_PARAM_SHAPES:
        try:
            return client.chat.completions.create(model=model, messages=messages, **extra)
        except Exception as exc:  # noqa: BLE001 -- try the next shape; fail closed if all do
            last_exc = exc
    assert last_exc is not None  # loop body ran at least once
    raise last_exc


def _slm_judge(
    response: str, rubric: str, threshold: float, options: dict[str, Any]
) -> JudgeResult:
    """SLM judge via an OpenAI-compatible or Azure endpoint. The endpoint
    defaults to the agent's own configured model (see _build_judge_client);
    set PARAPET_SLM_JUDGE_* to use a dedicated small model instead. Raises on
    transport/config error so the caller fails closed; a parseable-but-ambiguous
    answer fails via _parse_verdict rather than raising."""
    client, model = _build_judge_client(options)
    completion = _judge_completion(
        client,
        model,
        [
            {"role": "system", "content": _RUBRIC_SYSTEM},
            {"role": "user", "content": f"RULE:\n{rubric}\n\nASSISTANT RESPONSE:\n{response}"},
        ],
    )
    content = completion.choices[0].message.content or ""
    return _parse_verdict(content, threshold)


# backend id -> callable. 'slm' is always registered (its optional deps are
# imported lazily). Same additive-registry shape as groundedness.BACKENDS.
JudgeBackend = Callable[[str, str, float, dict[str, Any]], JudgeResult]
JUDGES: dict[str, JudgeBackend] = {"slm": _slm_judge}


def judge_response(
    response: str,
    rubric: str,
    *,
    backend: str = "slm",
    threshold: float = DEFAULT_THRESHOLD,
    options: dict[str, Any] | None = None,
) -> JudgeResult:
    """Run one judge backend. Unknown backend raises (unlike groundedness's
    fall-through) -- a judge is only ever configured with an explicit backend,
    and silently swapping it would change what "governed" means."""
    fn = JUDGES.get(backend)
    if fn is None:
        raise RuntimeError(f"unknown SLM-judge backend: {backend!r}")
    return fn(response, rubric, threshold, options or {})


# ── bundle-configured post-stage check (wired into maf.py) ───────────────

CONFIG_FILENAME = "response_judge.json"


@dataclass
class _JudgeEntry:
    library_id: str
    context_key: str
    backend: str
    rubric: str
    threshold: float
    model: str | None
    base_url: str | None


@dataclass
class JudgeEvalResult:
    """context: flat top-level keys the post-stage Cedar rule gates on, each
    True iff that judge FAILED the response (a real Cedar bool). errors:
    non-empty means a configured backend could not run -- maf.py turns that
    into a hard deny before Cedar, the same fail-closed contract as the tier-2
    scanners, so a missing verdict can never read as 'passed'."""

    context: dict[str, bool]
    errors: tuple[str, ...]


class JudgeConfig:
    """Loads the control plane's response_judge.json (rendered by
    parapetai_control.content_checks.render_response_judge_json) and runs the
    selected SLM judges against a model response at the post stage. Empty until
    a bundle actually selects a judge -- evaluate_post() is then a harmless
    no-op returning no context and no errors."""

    def __init__(self) -> None:
        self._entries: list[_JudgeEntry] = []

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
        entries: list[_JudgeEntry] = []
        for e in data if isinstance(data, list) else []:
            try:
                entries.append(
                    _JudgeEntry(
                        library_id=str(e["library_id"]),
                        context_key=str(e["context_key"]),
                        backend=str(e.get("backend", "slm")),
                        rubric=str(e["rubric"]),
                        threshold=float(e.get("threshold", DEFAULT_THRESHOLD)),
                        model=(str(e["model"]) if e.get("model") else None),
                        base_url=(str(e["base_url"]) if e.get("base_url") else None),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._entries = entries

    def evaluate_post(self, response: str) -> JudgeEvalResult:
        """Judge `response` for every selected rubric. A backend that raises is
        captured as an error (caller denies); otherwise the check's context_key
        is set True iff the judge FAILED the response."""
        context: dict[str, bool] = {}
        errors: list[str] = []
        for entry in self._entries:
            try:
                result = judge_response(
                    response,
                    entry.rubric,
                    backend=entry.backend,
                    threshold=entry.threshold,
                    options={"model": entry.model, "base_url": entry.base_url},
                )
            except Exception as exc:  # noqa: BLE001 -- fail closed: any error -> caller denies
                errors.append(f"{entry.library_id}: SLM judge raised: {exc}")
                continue
            context[entry.context_key] = not result.passed
        return JudgeEvalResult(context, tuple(errors))
