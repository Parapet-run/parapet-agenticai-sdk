# Exceptions

`parapetai_agent._exceptions` — one governance exception, importable from
the base install with no agent-framework dependency. Both the
framework-neutral facade (`Governor`) and the MAF adapter raise it, so a
denial is a single catchable type no matter which entry point produced
it. Re-exported as `parapetai_agent.GovernanceDenied` /
`parapetai_agent.GovernanceReviewRequired`.

## `GovernanceDenied`

```python
class GovernanceDenied(Exception):
    def __init__(self, decision: Decision) -> None: ...
```

Raised when a governance decision denies. Carries the Cedar `decision`
(verdict, reason, determining policy, stage) so a caller can inspect why.

| Attribute | Type | Meaning |
|---|---|---|
| `decision` | [`Decision`](decision.md) | The full decision that caused the denial. |

Message: `f"Blocked by governance policy: {decision.reason}"`.

```python
from parapetai_agent import GovernanceDenied

try:
    gov.authorize_tool("delete_account", {})
except GovernanceDenied as exc:
    print(exc.decision.effect)               # "deny"
    print(exc.decision.reason)
    print(exc.decision.determining_policies)  # cedarpy's own policy id(s)
```

## `GovernanceReviewRequired`

```python
class GovernanceReviewRequired(GovernanceDenied):
    def __init__(
        self, decision: Decision, *, review_id: str | None = None, fingerprint: str | None = None,
    ) -> None: ...
```

Raised when a call was **held for a human** rather than refused outright
— the `REVIEW` decision outcome (see [ADR 0008](../adr/0008-review-decision-outcome.md)).

**A subclass of `GovernanceDenied`, deliberately.** Every `except
GovernanceDenied:` written before approvals existed keeps blocking a held
call, and no caller starts executing one just by upgrading the SDK — the
affordance is additive; the failure mode of code that doesn't know about
`REVIEW` is always "blocked," never "executed."

| Attribute | Type | Meaning |
|---|---|---|
| `decision` | [`Decision`](decision.md) | The decision that triggered the review. |
| `review_id` | `str \| None` | `None` when the call was reviewable but the control plane could not be reached to queue it — still a deny, with nothing to poll: `Governor.wait_for_approval()` refuses it immediately in that case. |
| `fingerprint` | `str \| None` | The call's content fingerprint. Needed together with `review_id` to collect a grant. |

Message: `f"Held for approval: {decision.reason}{held}"`, where `held` is
`f" (review {review_id})"` when queued, or
`" (not queued: control plane unreachable)"` otherwise.

### Resolving a held call

```python
from parapetai_agent import GovernanceReviewRequired

try:
    gov.authorize_tool("wire_transfer", {"amount": 50000})
except GovernanceReviewRequired as held:
    approved = gov.wait_for_approval(held, timeout=300.0, poll_interval=2.0)
    if approved:
        ...  # proceed exactly once
```

`Governor.wait_for_approval()` takes the raised exception itself (not a
bare `review_id`), because collecting a grant needs the call's fingerprint
too, and the exception already carries both — passing them separately
would let a caller collect one review's grant while about to perform a
*different* call. It **blocks**, polling (not holding an open connection —
an approval takes as long as a human takes) until approved, denied,
expired, or `timeout` elapses, and returns `False` for every outcome
except "approved AND collected." `False` is always safe: it means the
local deny still stands. This is opt-in blocking — the default (not
calling `wait_for_approval()`) is to raise and return immediately, so a
~1ms governance check doesn't silently become a multi-minute one.

## Why one exception type, not several

Both exceptions live in `_exceptions.py`, imported by `govern.py` and
`maf.py` without either depending on the other's framework. Catching
`GovernanceDenied` — not `GovernanceReviewRequired`, not something
framework-specific — is correct in every context that only needs to know
"was this blocked," regardless of whether the block was a hard deny or a
held review. Catch `GovernanceReviewRequired` specifically only where you
intend to act on the review affordance (call `wait_for_approval()`, queue
a ticket for a human, etc.) — everywhere else, `GovernanceDenied` alone is
the right and sufficient catch.
