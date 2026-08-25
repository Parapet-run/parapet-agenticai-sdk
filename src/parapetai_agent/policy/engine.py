"""Cedar policy engine with atomic hot reload.

Direct on cedarpy. We deliberately do NOT use agentmesh.governance.cedar --
see CLAUDE.md for the rationale. The behaviours that wrapper provided and we
reimplement here are: request shaping, and nothing else. The behaviours it
lacked and we require are: reload, generation tracking, and fail-closed
semantics that survive a bad bundle.

INVARIANTS (do not relax without an ADR):
  - Cedar is default-deny. No matching `permit` means Deny.
  - `forbid` beats `permit` unconditionally. Policy file order is irrelevant.
  - A failed reload retains the previous generation. Never empty the set.
  - Evaluation errors deny.

STAGE/ACTION ANNOTATIONS (see docs/adr/0006): Cedar's own decision stays
strictly binary (permit/forbid -> Allow/Deny) -- this module never forks
Cedar's implementation or spec to add a third outcome. Two annotation keys
carry additional, purely additive meaning for an in-process caller like
parapetai-agent, resolved entirely OUTSIDE Cedar's own evaluation:
  - `@stage("pre"|"post")` on any policy scopes it to evaluate()'s pre- or
    post-call variant (see _split_policies_by_stage). No annotation means
    the policy applies to both -- never a special case to remember.
  - `@action("alter")` + `@alter_with("<name>")` on a `permit` names a
    transform a caller should apply to the content before letting it
    propagate, when that policy is the one that allowed the request.
    Never meaningful on a `forbid` -- a hard deny is never softened by an
    annotation. PolicyEngine only ever surfaces these as raw
    Decision.annotations; it has no opinion on what "alter" means.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cedarpy
import structlog

log = structlog.get_logger(__name__)


class PolicyLoadError(RuntimeError):
    """A policy set could not be built."""


@dataclass(frozen=True, slots=True)
class Decision:
    """Normalised decision. Mirrors the AGT decision record shape for interop."""

    allowed: bool
    effect: str  # allow | deny
    reason: str
    policy_generation: int
    evaluation_ms: float
    determining_policies: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    # Merged Cedar annotations (e.g. @action("alter"), @alter_with("...")) of
    # whichever policy(ies) determined an ALLOWED decision. Empty on a deny --
    # a forbid is never softened by an annotation, see engine.py module
    # docstring's stage/action design note. PolicyEngine stays unaware of what
    # "alter" means; it only ever surfaces raw Cedar annotation data. See
    # docs/adr/0006-cedar-policy-stage-and-action-annotations.md.
    annotations: dict[str, str] = field(default_factory=dict)

    def to_audit_record(
        self, *, principal: str, action: str, resource: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        record = {
            "principal": principal,
            "action": action,
            "resource": resource,
            "context": context,
            "decision": self.effect,
            "reason": self.reason,
            "determining_policies": list(self.determining_policies),
            "policy_generation": self.policy_generation,
            "evaluation_ms": round(self.evaluation_ms, 3),
        }
        if self.annotations:
            record["annotations"] = dict(self.annotations)
        return record


class PolicyEngine:
    """Holds a compiled Cedar policy set; supports atomic swap on reload."""

    def __init__(self, policy_dir: str | Path, entities_path: str | Path | None = None) -> None:
        self._policy_dir = Path(policy_dir)
        self._entities_path = Path(entities_path) if entities_path else None
        self._lock = threading.RLock()
        self._policies: str | None = None
        self._pre_policies: str | None = None
        self._post_policies: str | None = None
        self._annotations: dict[str, dict[str, str]] = {}
        self._pre_annotations: dict[str, dict[str, str]] = {}
        self._post_annotations: dict[str, dict[str, str]] = {}
        self._entities: list[dict[str, Any]] = []
        self._generation = 0
        self._digest = ""
        self._file_count = 0
        self.reload()  # raises on initial failure -- there is no fallback yet

    # ── loading ──────────────────────────────────────────────────────

    def _build(self) -> tuple[str, list[dict[str, Any]], str, int]:
        if not self._policy_dir.is_dir():
            raise PolicyLoadError(f"policy dir missing: {self._policy_dir}")

        # rglob so nested bundle layouts work. AGT's own loader uses a flat
        # non-recursive glob and silently ignores subdirectories -- we do not.
        sources = sorted(self._policy_dir.rglob("*.cedar"))
        if not sources:
            raise PolicyLoadError(f"no .cedar files under {self._policy_dir}")

        try:
            content = "\n\n".join(p.read_text() for p in sources)
        except OSError as exc:
            raise PolicyLoadError(f"unreadable policy file: {exc}") from exc

        entities: list[dict[str, Any]] = []
        if self._entities_path:
            if not self._entities_path.exists():
                raise PolicyLoadError(f"entities file missing: {self._entities_path}")
            try:
                entities = json.loads(self._entities_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise PolicyLoadError(f"bad entities file: {exc}") from exc
            if not isinstance(entities, list):
                raise PolicyLoadError("entities must be a JSON list")

        digest = hashlib.sha256(
            (content + json.dumps(entities, sort_keys=True)).encode()
        ).hexdigest()[:16]
        return content, entities, digest, len(sources)

    def reload(self, *, force: bool = False) -> dict[str, Any]:
        """Rebuild from disk. Returns a status dict; does not raise after first load."""
        try:
            content, entities, digest, count = self._build()
        except PolicyLoadError as exc:
            if self._policies is None:
                raise
            log.error("policy_reload_rejected", error=str(exc), generation=self._generation)
            return {"status": "failed", "error": str(exc), **self.status}
        return self._apply(content, entities, digest, count, force=force)

    def load_from_bundle(self, files: dict[str, str]) -> dict[str, Any]:
        """Rebuild directly from an in-memory bundle (parapetai_agent.control_plane's
        fetched {filename: content} dict) -- no disk round trip. Same
        fail-closed guarantees as reload() (shares _apply()): a bad bundle
        is rejected and the previous policy set keeps serving, never
        raising once the first load has succeeded.

        Exists so a poller that already has the fetched content in hand
        can apply it immediately instead of writing to disk and depending
        on a separate mechanism (a file watcher) to notice the write and
        call reload() -- that indirection is real disk persistence's job
        (a restart while the control plane is briefly unreachable still
        has something to load), not the live-update path's. Callers that
        want persistence too should still write `files` to disk
        themselves (e.g. via parapetai_agent.control_plane.sync_bundle_to_disk) --
        this method only ever touches memory.
        """
        # Mirrors reload()'s own try/except around _build(): a parsing
        # failure here must degrade to a "failed" status once the engine
        # has already loaded once, not raise -- otherwise a malformed
        # bundle from a live poller would crash the poller loop instead of
        # just being rejected, keeping the last-good policy in place.
        try:
            cedar_files = {name: text for name, text in files.items() if name.endswith(".cedar")}
            if not cedar_files:
                raise PolicyLoadError("bundle has no .cedar files")
            content = "\n\n".join(text for _, text in sorted(cedar_files.items()))

            entities: list[dict[str, Any]] = []
            entities_raw = files.get("entities.json")
            if entities_raw:
                try:
                    entities = json.loads(entities_raw)
                except json.JSONDecodeError as exc:
                    raise PolicyLoadError(f"bad entities in bundle: {exc}") from exc
                if not isinstance(entities, list):
                    raise PolicyLoadError("entities must be a JSON list")
        except PolicyLoadError as exc:
            if self._policies is None:
                raise
            log.error(
                "policy_load_from_bundle_rejected", error=str(exc), generation=self._generation
            )
            return {"status": "failed", "error": str(exc), **self.status}

        digest = hashlib.sha256(
            (content + json.dumps(entities, sort_keys=True)).encode()
        ).hexdigest()[:16]
        return self._apply(content, entities, digest, len(cedar_files))

    def _apply(
        self,
        content: str,
        entities: list[dict[str, Any]],
        digest: str,
        count: int,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Shared by reload() (disk-sourced) and load_from_bundle()
        (memory-sourced): unchanged-shortcut, split-by-@stage, compile-check
        all three variants, atomic swap. Identical fail-closed behavior
        regardless of where content came from -- a bad update here is
        rejected exactly like a bad file on disk, previous generation keeps
        serving. One bad @stage-filtered variant rejects the WHOLE reload,
        same as a bad full-set compile always has -- a bundle that compiles
        as a whole must never ship a broken pre- or post-only subset."""
        if not force and digest == self._digest and self._policies is not None:
            return {"status": "unchanged", **self.status}

        try:
            pre_content, post_content, annotations, pre_annotations, post_annotations = (
                _split_policies_by_stage(content)
            )
        except Exception as exc:
            if self._policies is None:
                raise PolicyLoadError(f"initial stage split failed: {exc}") from exc
            log.error("policy_stage_split_rejected", error=str(exc), generation=self._generation)
            return {"status": "failed", "error": str(exc), **self.status}

        for label, variant in (("full", content), ("pre", pre_content), ("post", post_content)):
            error = _probe_compile(variant, entities)
            if error is None:
                continue
            if self._policies is None:
                raise PolicyLoadError(f"initial policy compile failed ({label}): {error}")
            log.error(
                "policy_compile_rejected",
                variant=label,
                error=error,
                generation=self._generation,
            )
            return {"status": "failed", "error": f"{label}: {error}", **self.status}

        with self._lock:
            self._policies = content
            self._pre_policies = pre_content
            self._post_policies = post_content
            self._annotations = annotations
            self._pre_annotations = pre_annotations
            self._post_annotations = post_annotations
            self._entities = entities
            self._digest = digest
            self._file_count = count
            self._generation += 1

        log.info("policy_loaded", generation=self._generation, files=count, digest=digest)
        return {"status": "reloaded", **self.status}

    # ── evaluation ───────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        principal: str,
        action: str,
        resource: str,
        context: dict[str, Any],
        stage: str | None = None,
    ) -> Decision:
        import time

        started = time.perf_counter()

        if stage is not None and stage not in ("pre", "post"):
            # Not a data condition (attacker-controlled payload) -- a caller
            # passing an unrecognised stage literal is a programming error.
            # Still returned as a Decision, never raised, so evaluate()'s
            # fail-closed contract (invariants 1/2) holds for this too.
            with self._lock:
                generation = self._generation
            elapsed = (time.perf_counter() - started) * 1000
            msg = f"unknown stage: {stage!r}"
            return Decision(False, "deny", msg, generation, elapsed, errors=(msg,))

        with self._lock:
            generation = self._generation
            entities = self._entities
            if stage is None:
                policies, stage_annotations = self._policies, self._annotations
            elif stage == "pre":
                policies, stage_annotations = self._pre_policies, self._pre_annotations
            else:
                policies, stage_annotations = self._post_policies, self._post_annotations

        if policies is None:  # unreachable; __init__ raises
            return Decision(False, "deny", "no policy loaded", generation, 0.0)

        try:
            response = cedarpy.is_authorized(
                request={
                    "principal": _qualify(principal, "Agent"),
                    "action": _qualify(action, "Action"),
                    "resource": _qualify(resource, "Resource"),
                    "context": _scalarise(context),
                },
                policies=policies,
                entities=entities,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            log.exception("policy_evaluation_error")
            # FAIL CLOSED.
            return Decision(
                False, "deny", f"evaluation error: {exc}", generation, elapsed, errors=(str(exc),)
            )

        elapsed = (time.perf_counter() - started) * 1000
        allowed = response.decision == cedarpy.Decision.Allow
        diagnostics = getattr(response, "diagnostics", None)
        determining = tuple(getattr(diagnostics, "reasons", None) or ())
        eval_errors = tuple(str(e) for e in (getattr(diagnostics, "errors", None) or ()))

        # Only surfaced for an ALLOW -- a forbid is never softened by an
        # annotation (see module docstring). Merged in determining-policy
        # order; last write wins on a key collision across multiple
        # determining policies, an edge case a well-formed bundle shouldn't
        # produce but that must not raise if it does.
        resolved_annotations: dict[str, str] = {}
        if allowed:
            for policy_id in determining:
                resolved_annotations.update(stage_annotations.get(policy_id, {}))

        return Decision(
            allowed=allowed,
            effect="allow" if allowed else "deny",
            reason="permitted" if allowed else "denied: no permit matched or forbid applied",
            policy_generation=generation,
            evaluation_ms=elapsed,
            determining_policies=determining,
            errors=eval_errors,
            annotations=resolved_annotations,
        )

    @property
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation": self._generation,
                "policy_files": self._file_count,
                "digest": self._digest,
                "policy_dir": str(self._policy_dir),
            }


def _qualify(value: str, default_type: str) -> str:
    return value if "::" in value else f'{default_type}::"{value}"'


def _probe_compile(policies: str, entities: list[dict[str, Any]]) -> str | None:
    """Returns an error string if `policies` fails to compile, else None.

    Same throwaway-request technique _apply() has always used for the full
    set: cedarpy surfaces parse errors via diagnostics.errors on a
    NoDecision response, not as a raised exception -- check both. Extracted
    so _apply() can run the identical check against the full set and both
    @stage-filtered variants without repeating it three times.
    """
    try:
        probe = cedarpy.is_authorized(
            request={
                "principal": 'Agent::"__probe__"',
                "action": 'Action::"__probe__"',
                "resource": 'Resource::"__probe__"',
                "context": {},
            },
            policies=policies,
            entities=entities,
        )
        probe_errors = tuple(getattr(getattr(probe, "diagnostics", None), "errors", None) or ())
        if probe_errors:
            return "; ".join(probe_errors)
    except Exception as exc:
        return str(exc)
    return None


_EMPTY_POLICY_JSON = '{"templates":{},"staticPolicies":{},"templateLinks":[]}'


_AnnotationMap = dict[str, dict[str, str]]


def _split_statements(content: str) -> list[str]:
    """Split concatenated Cedar source into individual policy-statement texts,
    in source (== cedarpy parse) order. Walks the source tracking string
    literals and `//` line comments, so a ';', '//' or '@' inside a string is
    never mistaken for a terminator/comment, and splits on each top-level ';'
    (Cedar's only statement terminator). Comment/whitespace-only spans (a file
    header, blank lines between policies) carry no policy and are dropped; a
    policy keeps its own leading annotations, which sit after the previous ';'
    in the same chunk. Returns each surviving statement's ORIGINAL surface
    text -- the whole point (see _split_policies_by_stage)."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(content)
    in_str = in_comment = False
    while i < n:
        c = content[i]
        if in_comment:
            buf.append(c)
            if c == "\n":
                in_comment = False
            i += 1
        elif in_str:
            buf.append(c)
            if c == "\\" and i + 1 < n:  # escaped char -- take it verbatim
                buf.append(content[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            buf.append(c)
            i += 1
        elif c == "/" and i + 1 < n and content[i + 1] == "/":
            in_comment = True
            buf.append(c)
            i += 1
        elif c == ";":
            buf.append(c)
            statements.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    tail = "".join(buf)
    if tail.strip():
        statements.append(tail)

    def _carries_policy(stmt: str) -> bool:
        code = "\n".join(line.split("//", 1)[0] for line in stmt.splitlines())
        return "permit" in code or "forbid" in code

    return [s.strip() for s in statements if _carries_policy(s)]


def _split_policies_by_stage(
    content: str,
) -> tuple[str, str, _AnnotationMap, _AnnotationMap, _AnnotationMap]:
    """Splits one concatenated Cedar source into pre-stage and post-stage
    variants, keyed by an optional `@stage("pre"|"post")` annotation --
    absent on a policy, it's included in BOTH variants (the "no annotation
    = applies to both" contract). Also returns a policy-id -> annotations
    map for each of the three variants (full, pre, post), for
    Decision.annotations.

    Each variant keeps the ORIGINAL surface text of its matching policies (via
    _split_statements), and NEVER reconstructs Cedar from a filtered JSON policy
    set. cedarpy.policies_from_json_str cannot faithfully re-serialise every
    policy it can PARSE -- a long `||` chain (a ~60-clause demo-scope topic
    filter) makes it drop that policy or fail the whole reconstruction, which
    silently took the un-annotated base `model_call` permit down with it and
    default-denied every post-stage evaluation (found live: buffered responses
    blocked with "no permit matched" regardless of the judge verdict). Reading
    annotations from cedarpy's JSON is faithful (the parse direction is fine);
    only the RE-serialisation is broken, so we avoid it and keep original text.

    cedarpy re-numbers a variant's policies positionally (policy0, policy1, ...)
    in survival order when it later compiles the variant text, so each variant's
    annotation map is built from ITS OWN survival order, never sliced from the
    full set's ids -- otherwise Decision.determining_policies would look up the
    wrong policy's annotations. _split_statements preserves source (== parse)
    order, so statement[i] aligns with the full set's policy{i}; a count
    mismatch means the source could not be split to match cedarpy's own parse
    (e.g. a bundle using Cedar templates, which this splitter does not model)
    and is rejected (fail closed) rather than shipping a misaligned variant.
    """
    full = (
        json.loads(cedarpy.policies_to_json_str(content))
        if content.strip()
        else json.loads(_EMPTY_POLICY_JSON)
    )
    static: dict[str, dict[str, Any]] = full.get("staticPolicies", {})
    full_annotations = {pid: obj.get("annotations", {}) for pid, obj in static.items()}
    ordered = list(static.values())  # policy0, policy1, ... in parse order

    statements = _split_statements(content)
    if len(statements) != len(ordered):
        raise PolicyLoadError(
            f"stage split alignment mismatch: {len(statements)} source statement(s) "
            f"vs {len(ordered)} parsed policy(ies) -- refusing to ship a misaligned variant"
        )

    def _variant(keep: Any) -> tuple[str, dict[str, dict[str, str]]]:
        survivors = [
            (stmt, obj.get("annotations", {}))
            for stmt, obj in zip(statements, ordered)
            if keep(obj.get("annotations", {}))
        ]
        text = "\n".join(stmt for stmt, _ in survivors)
        annotations = {f"policy{i}": ann for i, (_, ann) in enumerate(survivors)}
        return text, annotations

    pre_content, pre_annotations = _variant(lambda ann: ann.get("stage") in (None, "pre"))
    post_content, post_annotations = _variant(lambda ann: ann.get("stage") in (None, "post"))
    return pre_content, post_content, full_annotations, pre_annotations, post_annotations


def _cedar_leaf(value: Any) -> str | int | bool:
    """Coerce one value into something cedarpy accepts as a context leaf.

    Cedar's Long is a 64-bit integer with no native float/decimal JSON
    mapping: a bare Python float in the context makes cedarpy's
    is_authorized() return a Deny with diagnostics.errors ==
    ("failed to parse schema from request",) -- verified directly against
    cedarpy, not assumed (a tool call with a float argument, e.g.
    latitude/longitude, hit this for real). That Deny still fails closed
    correctly (invariant 1) and Decision.errors still gets populated
    (invariant 2), but it is a parse error, not a policy decision --
    stringify floats so ordinary numeric tool/policy inputs don't silently
    turn into unexplained denies.
    """
    if isinstance(value, bool) or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return str(value)
    return str(value)[:4096]


def _nested_cedar_value(value: Any) -> str | int | bool:
    """Same float handling as _cedar_leaf, for a value one level inside a
    dict/list -- a further-nested dict/list/tuple/set is JSON-stringified
    (not repr'd) so a policy's `like` containment check still works against
    readable JSON text."""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value)[:4096]
    return _cedar_leaf(value)


def _scalarise(context: dict[str, Any]) -> dict[str, Any]:
    """Cedar context values must be scalars, records of scalars, or sets.

    Nested structures are JSON-stringified rather than dropped so a policy can
    still do a containment check (`like "*DROP*"`). Numeric comparison on a
    stringified value will not work -- project a typed field upstream instead.
    """
    out: dict[str, Any] = {}
    for key, value in context.items():
        if value is None:
            continue
        if isinstance(value, dict):
            out[key] = {k: _nested_cedar_value(v) for k, v in value.items()}
        elif isinstance(value, (list, tuple, set)):
            out[key] = [_nested_cedar_value(v) for v in value]
        else:
            out[key] = _cedar_leaf(value)
    return out
