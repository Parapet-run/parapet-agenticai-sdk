# ADR 0006 — Cedar policy stage/action annotations for pre/post governance

Status: accepted

## Context

Every enforcement point built so far — the standalone HTTP gateway
(`gateway/src/parapetai_gateway/server/app.py`) and parapetai-agent's MAF middleware
(`parapetai-agent/src/parapetai_agent/maf.py`) — evaluated a single Cedar decision
before the underlying call: `Snapshot` (`parapetai-agent/src/parapetai_agent/
providers/parsers.py`) is documented as "a normalised view of one inbound
request"; nothing in the Cedar `action` vocabulary (`model_call`/
`tool_call`/`http_request`) or the Cedar sources under `policies/` had any
notion of a response/output-side decision.

parapetai-agent's in-process integration doesn't share the gateway's constraint
that makes a second, output-side decision architecturally hostile
(invariant 6: never buffer an SSE relay). MAF's own `ChatContext`/
`FunctionInvocationContext.result` is observable, and settable, *after*
`await call_next()` — a real hook for a non-streaming response. Two new
requirements followed directly from having that hook:

1. **Which stage a policy applies to** (pre-call request, post-call
   response, or both) needed to be selectable per rule, driven by the
   control plane's rule-authoring UI, which renders the annotation into the
   signed bundle this SDK then loads.
2. **A post-call decision needed a richer outcome than allow/deny**:
   specifically, the ability to mutate a model response or tool result
   before it propagates into a *subsequent* call, so a bad payload doesn't
   become part of the next turn's input — without that content having
   already been irreversibly delivered (the streaming case, where it's
   too late either way).

## Decision

**Cedar's own decision stays strictly binary.** `permit`/`forbid` →
Allow/Deny is unchanged; `PolicyEngine.evaluate()`'s core `Decision.allowed`
contract does not change shape, and cedarpy is used exactly as before —
this is a hard constraint, not a preference: invariant 3 (default-deny,
`forbid` always beats `permit`) is defined in terms of that binary
decision, and doing anything else would mean re-deriving or forking that
guarantee outside Cedar's own engine.

**Both new capabilities are expressed as standard Cedar policy
annotations, not new Cedar constructs**:

- `@stage("pre"|"post")` scopes a policy to one of two filtered variants of
  the compiled policy set, built at `PolicyEngine` reload time via
  `cedarpy.policies_to_json_str()`/`policies_from_json_str()` (verified
  directly: cedarpy round-trips arbitrary custom annotations through its
  JSON policy representation, and does NOT preserve a policy's original
  positional id across a filter — a policy that was `policy2` in the full
  set can become `policy1` in a filtered variant, so each variant needs its
  own annotation map built from its own post-filter survival order, not
  sliced from the full set's). No annotation means the policy is included
  in *both* filtered variants — "applies to both" falls out of the
  filtering rule itself, never a special case a bundle author has to
  remember. `evaluate()` gained an optional `stage: str | None = None`
  parameter, default `None` meaning today's exact full-set behavior — every
  existing caller (the HTTP gateway included) is unaffected.
- `@action("alter")` + `@alter_with("<name>")` on a `permit` — never a
  `forbid`, a hard deny is never softened by an annotation — names a
  transform a caller should apply to the content before letting it
  propagate, when that policy is the one that allowed a post-call
  decision. `Decision` gained one generic field, `annotations: dict[str,
  str]`, merged from whichever policy(ies) determined an *allowed*
  decision (empty on a deny). `PolicyEngine` itself stays completely
  unaware of what "alter" means — it only ever surfaces raw Cedar
  annotation data; the semantics live one layer up, in
  `parapetai_agent.policy.hooks.GovernanceHook` (resolves `alter_with` from
  `Decision.annotations`) and `parapetai-agent`'s MAF middleware (applies a
  named transform from a caller-supplied registry, or fails closed to a
  deny if the name isn't registered).

**ALTER is post-call only.** Pre-call code paths never read
`HookResult.alter_with` — that omission *is* the entire enforcement
boundary for "ALTER only applies post-call," not a validation rule that
rejects a misplaced annotation. An `@action("alter")` on a pre-scoped
policy is simply inert everywhere except a post-call hook.

**OBSERVE is not a new decision type at all.** It's a control-plane
authoring concept: a rule marked "observe" renders as a plain `permit`
(optionally tagged `@action("observe")` purely for the control plane's own
UI bookkeeping) — parapetai-agent's runtime needs zero new logic for it, since it
behaves identically to any other allow.

**Streaming inherits DENY's existing audit-only limitation, not a new
tradeoff.** A streamed chat response can't be altered before chunks
already reached the caller, verified directly against
`agent_framework`'s `ChatMiddlewarePipeline.execute()`: `context.
stream_result_hooks` are wired onto the returned `ResponseStream` only
*after* the whole middleware chain (this one included) has returned, and
that hook itself only fires once the stream is fully finalized — which
requires the caller to have already consumed every chunk. So a streaming
post-call ALTER (or DENY) can only audit what happened
(`post_call_would_deny_streaming`/`post_call_would_alter_streaming`),
never block or rewrite it — the same physical fact that already made the
HTTP gateway's SSE relay unable to do output-side decisions at all
(invariant 6), independently re-confirmed here for parapetai-agent rather than
assumed to transfer.

## Alternatives considered

- **A `context.stage` value individual policies `when`-check**, instead of
  a policy-level annotation. Rejected: it would make "applies to both"
  require every policy author to remember to omit or handle a `when`
  clause correctly, and it couples the stage concept to the Cedar
  *evaluation* rather than to policy *selection* — the control plane
  authors the stage choice, it shouldn't have to also author defensive
  Cedar conditions to express it.
- **A fourth persisted decision value** (`OBSERVE`/`ALTER` as first-class
  outcomes threaded through `Decision.effect`/audit records). Rejected per
  explicit product direction: Cedar's decision stays binary, and both new
  behaviors collapse to something Cedar already understands (an allow,
  optionally annotated) plus caller-side interpretation — forking Cedar's
  own decision spec to add a third/fourth outcome was never on the table.
- **A parametric alter-transform mini-language embedded in the
  annotation** (e.g. a regex or field path carried directly in
  `@alter_with(...)`), instead of a named, adopter-registered callable.
  Rejected as premature: this repo has no real redaction/transform logic
  to generalize from yet (`DEFAULT_ALTER_TRANSFORMS` ships exactly one
  placeholder, `redact_all`) — a named-callable registry
  (`build_middleware(alter_transforms={...})`) is the minimal surface that
  works today and doesn't foreclose a richer spec later if a concrete need
  shows up.

## Consequences

- **A bundle with no `@stage`/`@action` annotations anywhere behaves
  identically to before this ADR**, at every existing call site — this
  was verified with a regression test
  (`parapetai-agent/tests/test_maf.py::TestPostCallRegressionWithRealBundle`)
  against the real `policies/` bundle, not just asserted.
- **parapetai-agent now runs two Cedar decisions per model/tool call** (pre and
  post), not one — visible in the audit log as two `decision` events per
  call, and doubling evaluation cost per call. Always on, no toggle: which
  Cedar rules actually do anything at each stage is controlled entirely by
  the bundle's annotations, never a parapetai-agent-side flag, so "some governance
  silently skipped" stays impossible (invariant 1).
- **An unresolved `@alter_with` name fails closed to a deny**, both for
  chat (`GovernanceDenied` raised) and tool calls (`context.result`
  substituted) — a control-plane-authored transform name that doesn't
  match anything an enforcing process registered is a blocked call with a
  clear reason in the audit log, never a silent pass-through of the
  original, unaltered content.
- **`gateway/` is unaffected.** It never passes `stage=` to `evaluate()`,
  so it keeps evaluating the full, unfiltered policy set exactly as
  before — this ADR's scope is `parapetai-agent` and control-plane's
  rule-authoring path only.
