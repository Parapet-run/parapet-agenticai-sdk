# ADR 0009 — The approval loop (SDK side)

Status: accepted

Consumes the primitive ADR 0008 created.

This repository owns the enforcement half: what a PEP does with a held call.
The approvals queue itself — storage, the console, the capability model,
audit — lives in the control plane and is not described here beyond the
protocol this SDK speaks to it.

## Context

ADR 0008 gave the engine a third outcome: a `forbid` annotated
`@action("review")` surfaces as `Decision(effect="review")` — a call dangerous
enough to need a person, not so dangerous it should never happen. It ended
with an explicit non-goal: nothing could *resolve* one. An agent whose call
was held was refused and had to retry after a human acted out of band.

Without a resume path the third outcome is only a differently-coloured deny.

The constraint that shapes everything below: **the control plane is never on
the decision path.** A PEP evaluates Cedar locally and keeps enforcing its
last bundle when the control plane is unreachable. An approval mechanism must
not trade that away.

## Decision

**The control plane goes on the approval path and never on the decision
path.** Cedar still decides, locally, that a call is reviewable. The control
plane records the request and a human's answer. A PEP that cannot reach it has
no approvals available and its own review stays a deny — fail-closed, exactly
as before this ADR. Unreachability can cost an approval; it can never soften
an enforcement.

**A held call returns a ticket; it does not block.** `authorize_tool()` raises
`GovernanceReviewRequired` carrying a `review_id` and returns immediately.
`Governor.wait_for_approval(held)` is opt-in for a caller that wants to block.
Blocking by default would turn a ~1ms governance check into a multi-minute
call pinning an agent thread, and would be unusable from an async framework.

**`GovernanceReviewRequired` subclasses `GovernanceDenied`.** Every
`except GovernanceDenied:` written before approvals existed keeps blocking a
held call, so no integration starts executing one by upgrading the SDK. This
is the exception-level restatement of why `Decision.allowed` stays `False` for
a review: the affordance is additive, and an unaware caller fails closed.

**A grant is single-use and bound to one exact call.** `review_fingerprint()`
hashes (agent, action, tool, canonical arguments); the same value is computed
when the review is raised and presented again when the grant is collected, and
the control plane refuses a mismatch. Approving "close INC-42" cannot be
replayed onto INC-43.

**`wait_for_approval()` returns `False` for every non-approval** — denied,
expired, never queued, control plane unreachable, HTTP refusal, timed out — so
a caller has exactly one thing to check and the safe answer is the default. It
stops immediately on a terminal state rather than polling a dead review until
the timeout.

**Prompt content never reaches the queue.** A tool call's arguments are sent as
an operator-facing preview — they are what the policy already matched on, and
an approver who cannot see which incident is being closed cannot meaningfully
approve closing it. `check_input`/`check_output` send a fingerprint and no
preview: their "arguments" are the prompt and the model's response. A digest is
not content, so the grant is still bound to that exact prompt.

## Alternatives considered

- **Block inside `authorize_tool()` until answered.** Rejected: pins a thread
  for as long as a human takes, hostile to async frameworks, and changes the
  latency profile of every governance call to accommodate the rare one. Kept
  as an opt-in helper, which costs nothing.
- **No client-side resume — approve in the console, let the agent retry.**
  Rejected: with nothing bound to the approval, the *next* matching call is
  allowed, which is a time-boxed pattern grant wearing a single-approval
  costume. The agent also cannot tell whether an approval happened.
- **Deliver grants through the existing bundle poll.** Rejected: bundles are
  policy, not per-call state; grants are per-call-instance and would bloat
  every bundle, and a ≤30s poll is poor latency for a person waiting at a
  console with an agent blocked.
- **Sign the grant so the PEP can verify it offline.** Rejected *for now*, not
  on principle: it removes a round trip but makes single-use enforcement much
  harder — a signed token is replayable unless the PEP keeps its own spend
  ledger. Revisit if approval latency ever outweighs one authoritative row.

## Consequences

- **A PEP with no control plane is unchanged.** `Governor.from_policy_dir()`
  sets no review client, so a review raises with `review_id=None` and
  `wait_for_approval()` refuses immediately. Approvals are an affordance a
  connected PEP gains, never a requirement local enforcement takes on.
- **`raise_on_deny=False` does not queue a review.** `Decision` is frozen
  (`frozen=True, slots=True`), so a non-raising return has nowhere to carry a
  `review_id`, and queueing anyway would leave rows an operator can see but
  the caller can never resolve. Such a caller opts in with
  `Governor.request_approval()`.
- **`gateway/` still refuses a review with HTTP 403 and does not queue one.**
  The proxy PEP cannot hold a client connection for a human any more than the
  SDK can, so participating needs a resume protocol of its own — return the
  `review_id` on the 403, accept it on the retry. Deliberately out of scope;
  ADR 0008's gateway consequences are unchanged.
- **Permission is an event, not a state.** The control plane reports `allowed`
  only for the collection that just succeeded. Deriving it from stored status
  was a real bug found by an end-to-end run and not by unit tests: a
  second collection of an already-spent grant reported allowed, so one
  approval could execute a held call twice.
