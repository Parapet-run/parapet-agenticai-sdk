# ADR 0008 — REVIEW as a third decision outcome

Status: accepted

Amends ADR 0006 (one rejected alternative; see "Relationship to ADR 0006").

## Context

The product has committed to a three-way decision surface — ALLOW / DENY /
REVIEW — as the thing that distinguishes Parapet from an allow/deny
gateway. Until this ADR, `Decision.effect` was strictly `allow`/`deny` and
nothing in the engine, the gateway, or the console could express "this call
is dangerous enough to need a person, but not so dangerous that it should
never happen."

The gap matters in three places at once, which is why it is worth changing
the decision core rather than special-casing it per enforcement point:

1. **Marketplace/product**: the whole pitch for governing an AI agent inside
   a system of record (Jira, ServiceNow, GitHub) is that the interesting
   operations are neither obviously safe nor obviously forbidden.
   `transition_issue` to Done, a bulk update over some threshold, a workflow
   change — a blanket deny makes the agent useless and a blanket allow makes
   the product pointless.
2. **Enterprise**: "every AI write requires approval" is a much weaker claim
   than "only the risky ones do", and the difference is only expressible if
   the engine can return a third outcome.
3. **Engine honesty**: `policies/20-tools.cedar` already encodes real
   argument-level judgement (`update_incident` with `state=closed` is denied
   because it bypasses the resolution process). Several of those rules are
   morally "ask a human", and had to be written as hard denies because
   nothing else existed.

## Decision

**`@action("review")` on a `forbid` marks that deny as escalatable to a
human.** It surfaces as `Decision.effect == "review"`, resolved outside
Cedar's evaluation exactly as `@action("alter")` is.

**`Decision.allowed` stays `False` for a review.** This is the property that
makes REVIEW safe to add to a running deployment: every caller that only
ever checks `allowed` — which is every caller written before this ADR,
including `gateway/`'s own `if not decision.allowed` branch — keeps blocking
a review exactly as it blocked a deny. Adding REVIEW cannot make an existing
integration less safe. `allowed` is deliberately not a tri-state, and
`requires_review` is a property derived from `effect` rather than a second
stored field, so the two can never disagree.

**Reviewability requires all three of the following**, each an independent
fail-closed guard (`_is_reviewable`):

1. **No evaluation errors.** Invariant 2 requires a fail-closed deny to stay
   distinguishable from a real policy decision. A broken bundle must never
   present to an operator as "a human can approve this" — there is no policy
   intent behind it to approve.
2. **A non-empty determining set.** An empty `diagnostics.reasons` is
   Cedar's bare default-deny: no rule matched, so no author granted a review
   affordance. Reviewability is opted into by a rule, never inferred from
   the absence of one.
3. **Unanimity — *every* determining policy carries `@action("review")`.**
   Verified directly against cedarpy 4.x: two matching forbids come back as
   `['policy1', 'policy0']` — *both* present, and *not* in source order. A
   hard `forbid` matching alongside a reviewable one means some rule said
   "never", and unanimity is what stops a human approval from authorising an
   action that rule forbade. Without this guard, adding a permissive review
   rule anywhere in a bundle would silently make unrelated hard denies
   approvable — a privilege-escalation bug, not a cosmetic one.

**Annotations resolve on a review as well as an allow, never on a hard
deny.** A review carries the policy author's reviewer-facing detail
(`@review_reason`, `@risk_score`, …) and that is the only channel by which
it reaches an approvals queue. A hard forbid is still never softened *or*
explained by an annotation. `PolicyEngine` remains unaware of what any
annotation value means.

**`Decision.reason` names the policy's own `@id`,** not cedarpy's positional
`policy0`/`policy1`, which shift whenever a bundle gains or loses a rule and
are useless in an audit record or an approvals queue.

## Relationship to ADR 0006

ADR 0006 rejected "a fourth persisted decision value (`OBSERVE`/`ALTER` as
first-class outcomes threaded through `Decision.effect`/audit records)". That
rejection stands **for `OBSERVE` and `ALTER`**, and for the reason given
there: both collapse to something Cedar already understands — an allow,
optionally annotated — so promoting them to their own effect would have
added a decision value carrying no decision.

REVIEW is not that case. It does not collapse to an allow (the call does not
execute) and it does not collapse to a deny (a human can still authorise
it). It is a genuinely distinct terminal state of one evaluation, and an
audit record that flattened it to "deny" would make a held call
indistinguishable from a blocked one — which is precisely what an approvals
queue is built from.

**ADR 0006's hard constraint is untouched.** Cedar's own decision stays
strictly binary: cedarpy returns `Deny`, `permit`/`forbid` semantics are
unchanged, and invariant 3 (default-deny, `forbid` beats `permit`) is
defined in terms of that binary decision and still holds exactly. What
changed is Parapet's *normalised* outcome, which was always a layer above
Cedar's.

ADR 0006 also states "a hard deny is never softened by an annotation." That
still holds. A review **is a deny** at the enforcement point — `allowed` is
`False`, nothing executes. The annotation does not soften the decision; it
records that a separate, out-of-band human approval is permitted to
authorise the action later. That approval is a new authorization event, not
a retroactive weakening of this one.

## Alternatives considered

- **`@action("review")` on a `permit`.** Rejected as fail-open: Cedar would
  return Allow, so any caller that ignored the annotation — an older SDK, a
  third-party PEP, a bug — would execute the risky call. Putting the
  affordance on the `forbid` means the failure mode of every unaware caller
  is "blocked", not "executed".
- **A tri-state `Decision.allowed`.** Rejected: it silently changes the
  meaning of an existing field that every enforcement point branches on, so
  a caller compiled against the old contract could start executing held
  calls. A new *value* in `effect` plus an unchanged `allowed` is additive;
  a changed `allowed` is not.
- **"First determining policy wins" instead of unanimity.** Rejected: the
  probe showed cedarpy returns reasons in non-source order, so "first" is
  not a stable or meaningful choice, and picking the reviewable one would
  let a review rule override a hard forbid.
- **A separate `review` policy set evaluated before the main one.**
  Rejected: two policy sets means two reload paths, two annotation maps, and
  a new ordering question between them, for no expressiveness that an
  annotation on the existing set doesn't already give.

## Consequences

- **A bundle with no `@action("review")` anywhere behaves identically to
  before this ADR.** Covered by a regression test rather than asserted.
- **`gateway/` refuses a review with HTTP 403**, like a deny, and does not
  forward upstream — it has no approval workflow of its own. The distinction
  rides on the `x-parapetai-decision` header, a distinct JSON-RPC error code
  for MCP (`-32001` vs `-32000`), and a distinct `code` in the fallback
  error shape. HTTP status stays 403 on purpose: a 2xx would make a provider
  SDK try to deserialise a held call as a successful completion.
  *(Amended by ADR 0009: the gateway now has an approval workflow. The 403 and
  every distinction above are unchanged — what is added is a
  `x-parapetai-review-id` ticket on the refusal, which the client re-presents
  to collect its approval on the retry.)*
- **The console classifies review as its own verdict.** Before this, the
  verdict classifier tested `if not is_deny: verdict_kind = "allow"`, which
  would have rendered a held call with the green allow pill — a call that
  never executed shown to an operator as one that did.
- **`dashboard_stats` counts a review in the deny bucket** (`allow if
  decision == "allow" else deny`). Conservative and therefore safe, but it
  under-reports: a review is not a block. A three-series breakdown is
  deliberately left until the approvals queue exists to give the number
  meaning.
- **The engine can now express a held call, but nothing yet holds it.**
  There is no pending-approval store, no approve/deny action, and no resume
  path — an agent whose call is held is refused and must retry after a human
  approves out of band. That workflow is the next build and consumes this
  primitive; it is not part of this ADR.
- **This repository is the single source of truth for the engine.** REVIEW was
  first written against a second, internal copy of `policy/engine.py`, which
  meant the published SDK could not produce a review decision at all — the
  control plane could author policy the enforcing SDK was unable to execute.
  That copy has been merged here and retired. Anything that changes the
  decision path must land in this repository.
