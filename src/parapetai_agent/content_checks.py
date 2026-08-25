"""Tier-2 content checks: real pattern/entity detection against outgoing
prompt text, run IN-PROCESS by this SDK -- never by the standalone
gateway (see control-plane/src/parapetai_control/content_checks.py's own module
docstring for why tier 1 vs tier 2 split this way: tier 1 is Cedar `like`
matching alone, which needs no code here at all; tier 2 needs an actual
scanner run before Cedar ever sees a decision, and the control plane
decided this belongs in the SDK, not the gateway).

## Where this fits in the request path

ParapetChatMiddleware.process() (maf.py) calls ContentCheckConfig.evaluate()
on the outgoing snapshot BEFORE calling GovernanceHook.evaluate() -- the
result becomes `extra_context` merged into the Cedar context (see
hooks.py's own docstring for that param). A generated Cedar rule
(control-plane's 45-content-checks-tier2.cedar) then gates on
`context.<context_key>`, exactly the same shape a tier-1 rule gates on
`context.messages_preview`. context_key is a FLAT, top-level context
field name (e.g. "content_checks_pii_types"), never a nested path like
"content_checks.pii_types" -- see evaluate()'s own comment for why: a
list nested two levels inside context gets JSON-stringified by
PolicyEngine's _scalarise(), not kept as a real Cedar Set, which breaks
`.containsAny()` silently (a real bug caught by this module's own tests,
not a hypothetical).

## The fail-closed guarantee this module exists to uphold

A generated tier-2 Cedar rule is written as `context has <key> &&
context.<key>.containsAny([...])` -- deliberately simple, no extra "what
if this key is missing" defense INSIDE the Cedar text. That's safe ONLY
because of a guarantee this module (and its one caller in maf.py) upholds
outside Cedar entirely: if a scanner a bundle's content_checks.json
configures fails to run for any reason, evaluate() returns errors, and
the CALLER (maf.py) turns that into a hard deny BEFORE
GovernanceHook.evaluate() is ever invoked -- Cedar never runs, so it can
never "see" the missing key and treat it as absence-means-pass. Without
this, a scanner crash would silently degrade a selected tier-2 check
into a no-op (`context has <key>` false -> the forbid's `when` clause is
false -> nothing blocks), which is exactly the failure mode CLAUDE.md
invariant 1 exists to rule out. See _content_check_failure_decision in
maf.py for where that deny is built.

## Scanner backends

`regex_entities` is the only backend implemented here: real regex +
checksum detection (credit-card numbers are Luhn-validated, not just
digit-shaped), genuinely more than Cedar's `like` glob can express on its
own (no character classes, no quantifiers, no checksum). Deliberately
NOT Presidio or LLM Guard -- both need real ML dependencies (spaCy
models, transformer weights) that would make parapetai-agent's `content-checks`
extra a multi-hundred-MB download requiring model files fetched at
install/first-run time, which is real infrastructure this pass doesn't
attempt. SCANNERS below is a plain registry keyed by scanner_id
specifically so a heavier backend can be added later as ANOTHER key,
gated behind its own optional extra, without changing
ContentCheckConfig's interface at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from parapetai_agent.providers.parsers import Snapshot

log = structlog.get_logger(__name__)

BUNDLE_FILENAME = "content_checks.json"


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum -- the same validation Presidio's own
    CreditCardRecognizer runs, kept here rather than imported so this
    module has zero third-party dependencies. Distinguishes a real
    card-shaped number from an arbitrary 13-16 digit run (an order id, a
    phone number with punctuation stripped), the way a bare regex alone
    cannot."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _find_credit_card(text: str) -> bool:
    for match in re.finditer(r"\b(?:\d[ -]?){13,19}\b", text):
        digits = re.sub(r"[ -]", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return True
    return False


# entity_type -> detector. Each is a plain predicate over the preview
# text, not a match-position extractor -- this feature only ever needs
# "was this entity type present at all" (the generated Cedar rule is a
# boolean gate), never offsets/spans. US_SSN/AWS_ACCESS_KEY/EMAIL_ADDRESS
# are single-regex; CREDIT_CARD needs the Luhn pass above to avoid
# flagging any 13-19 digit run as a card number.
_ENTITY_DETECTORS: dict[str, Callable[[str], bool]] = {
    "US_SSN": lambda text: re.search(r"\b\d{3}-\d{2}-\d{4}\b", text) is not None,
    "CREDIT_CARD": _find_credit_card,
    "AWS_ACCESS_KEY": lambda text: re.search(r"\bAKIA[0-9A-Z]{16}\b", text) is not None,
    "EMAIL_ADDRESS": lambda text: (
        re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text) is not None
    ),
}


def _regex_entities_scanner(text: str, entity_types: list[str]) -> list[str]:
    """The `regex_entities` backend: returns the subset of `entity_types`
    actually found in `text`. An entity_type absent from _ENTITY_DETECTORS
    (a bundle referencing one this SDK version doesn't know about yet --
    e.g. produced by a newer control plane) is silently skipped, not an
    error -- matching content_check_library's own catalog forward-
    compatibility story: an unknown entity type can't be checked, but
    that's a narrower failure than refusing to run every OTHER configured
    entity type in the same entry."""
    return [e for e in entity_types if e in _ENTITY_DETECTORS and _ENTITY_DETECTORS[e](text)]


SCANNERS: dict[str, Callable[[str, list[str]], list[str]]] = {
    "regex_entities": _regex_entities_scanner,
}


@dataclass(frozen=True, slots=True)
class _ContentCheckEntry:
    library_id: str
    scanner_id: str
    entity_types: list[str]
    context_key: str


@dataclass(frozen=True, slots=True)
class ContentCheckEvalResult:
    """context: merge straight into GovernanceHook.evaluate(extra_context=...)
    on success. errors: non-empty means a CONFIGURED scanner could not run
    (unknown scanner_id, or the scanner itself raised) -- see this
    module's own docstring for why the caller must treat that as a hard
    deny rather than proceeding with partial context."""

    context: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


class ContentCheckConfig:
    """Holds this agent's tier-2 selections (parsed from a bundle's
    content_checks.json) and runs them. One instance per
    ParapetChatMiddleware, constructed empty (evaluate() is then a
    harmless no-op) and populated by load_from_bundle() -- same
    "safe when never configured" shape PolicyEngine's own base permits
    give tier 1."""

    def __init__(self) -> None:
        self._entries: list[_ContentCheckEntry] = []

    def load_from_bundle(self, files: dict[str, str]) -> None:
        """Mirrors PolicyEngine.load_from_bundle()'s own fail-closed
        contract: a missing content_checks.json means zero tier-2 checks
        (a valid, common state -- most agents will never select one), but
        a PRESENT and malformed one is rejected WITHOUT touching
        self._entries, so a bad control-plane push can't silently disable
        every tier-2 check an operator already turned on. Never raises --
        called from parapetai_agent.control_plane's poll loop via its generic
        on_bundle hook, which propagates a raising callback (see that
        module's own docstring); this callback specifically must not be
        the thing that takes down a poll cycle over one bad file."""
        raw = files.get(BUNDLE_FILENAME)
        if raw is None:
            self._entries = []
            return
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("content_checks.json must be a JSON list")
            entries = [
                _ContentCheckEntry(
                    library_id=e["library_id"],
                    scanner_id=e["scanner_id"],
                    entity_types=list(e["entity_types"]),
                    context_key=e["context_key"],
                )
                for e in parsed
            ]
        except (ValueError, KeyError, TypeError) as exc:
            log.error("content_checks_bundle_rejected", error=str(exc))
            return
        self._entries = entries

    def evaluate(self, snapshot: Snapshot) -> ContentCheckEvalResult:
        """Runs every configured entry against snapshot.messages_preview
        (the same capped preview tier-1 `like` rules already match
        against -- see control-plane's content_checks.py for the same
        2000-char-cap caveat, now also a detection-recall limit, not just
        a logging-fidelity one). Groups results by context_key (a flat,
        top-level context field name -- see this module's own docstring
        for why it must be flat): multiple entries CAN legitimately share
        one key (e.g. two different catalog selections both feeding
        "content_checks_pii_types"), in which case their found entity
        types are unioned rather than one overwriting the other."""
        if not self._entries:
            return ContentCheckEvalResult()
        text = snapshot.messages_preview
        by_key: dict[str, set[str]] = {}
        errors: list[str] = []
        for entry in self._entries:
            scanner = SCANNERS.get(entry.scanner_id)
            if scanner is None:
                errors.append(f"{entry.library_id}: unknown scanner {entry.scanner_id!r}")
                continue
            try:
                found = scanner(text, entry.entity_types)
            except Exception as exc:  # noqa: BLE001 -- see module docstring: must become a deny, not propagate raw
                errors.append(f"{entry.library_id}: scanner raised: {exc}")
                continue
            by_key.setdefault(entry.context_key, set()).update(found)
        if errors:
            return ContentCheckEvalResult(errors=tuple(errors))
        # FLAT top-level keys, deliberately -- NOT {"content_checks": {key:
        # [...]}}}. Verified live against cedarpy: PolicyEngine._scalarise()
        # only promotes a Python list to a real Cedar Set when it's a
        # TOP-LEVEL context value; a list found one level inside a nested
        # dict gets JSON-stringified instead (engine.py's own
        # _nested_cedar_value docstring says as much), which silently turns
        # `.containsAny(...)` into a Cedar type error -- Cedar then SKIPS
        # that policy as inapplicable rather than denying, exactly the
        # fail-open bypass this module's own docstring says must not
        # happen. A real, caught-live bug the first time this was wired
        # into an end-to-end test (parapetai-agent/tests/test_maf.py's
        # TestTier2ContentChecksEnforcement), not a hypothetical. So
        # context_key (from content_checks.json) must itself be a bare,
        # already-flat context field name -- e.g. "content_checks_pii_types"
        # -- and this dict IS the extra_context merged directly into
        # GovernanceHook.evaluate(), one key per (deduplicated) entry.
        return ContentCheckEvalResult(context={key: sorted(vals) for key, vals in by_key.items()})
