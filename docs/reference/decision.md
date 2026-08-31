# `Decision`

`parapetai_agent.policy.engine.Decision` — the single, framework-agnostic
verdict object every governed call produces. `Governor.check_input()` /
`.authorize_tool()` / `.check_output()`, `GovernedAgent`, and
`GovernedRunner` all return (or attach, via
[`GovernanceDenied.decision`](exceptions.md)) the exact same dataclass —
nothing about it is specific to any one framework.

```python
@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    effect: str  # "allow" | "deny" | "review"
    reason: str
    policy_generation: int
    evaluation_ms: float
    determining_policies: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def requires_review(self) -> bool: ...

    def to_audit_record(self, *, principal, action, resource, context) -> dict: ...
```

Frozen (immutable) and `slots=True` — a `Decision` is a value, never
mutated after Cedar produces it.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `allowed` | `bool` | **`False` for both `"deny"` and `"review"`** — a review has not been authorized by anything; it is a deny a human may later turn into an allow via a separate approval. Any code written before `REVIEW` existed, or that only ever checks `allowed`, keeps blocking a held call exactly as it blocked a plain deny. This is why `allowed` is not a tri-state. |
| `effect` | `str` | One of `"allow"`, `"deny"`, `"review"`. |
| `reason` | `str` | Human-readable explanation. For a plain `"allow"` this is the generic string `"permitted"`. For a plain (non-reviewable) `"deny"` it is the generic string `"denied: no permit matched or forbid applied"` — **not** the name of the specific policy that denied; see `determining_policies` below for that. For `"review"` it's `f"review required: {labels}"`, where `labels` resolves each determining policy's `@id("...")` annotation to a friendly name. |
| `policy_generation` | `int` | The policy engine's generation counter at evaluation time — increments on every successful reload/bundle apply. Useful for correlating a decision to exactly which policy version produced it. |
| `evaluation_ms` | `float` | Wall-clock time for the Cedar evaluation itself (around `cedarpy.is_authorized()`), in milliseconds. Does **not** include the model call, the tool call, or any content-check/groundedness scanning around it. |
| `determining_policies` | `tuple[str, ...]` | Cedar's own determining-policy id(s) — `diagnostics.reasons` from the underlying `cedarpy` evaluation. **These are `cedarpy`'s raw positional ids** (e.g. `"policy3"`, assigned in file-concatenation order across every loaded `.cedar` file), **not** the friendlier `@id("...")` annotation from the `.cedar` source. The `@id`-to-label resolution (`_policy_labels()`) is a private helper used only to build `reason` on the `review` path — a plain hard deny's `reason` stays generic (see above). |
| `errors` | `tuple[str, ...]` | Evaluation errors, if any (e.g. malformed context). Non-empty errors are part of what makes a decision fail closed. |
| `annotations` | `dict[str, str]` | Merged Cedar annotations (e.g. `@action("alter")`, `@alter_with("...")`, `@review_reason`, `@risk_score`) of whichever policy(ies) determined the decision — **populated for an `allow` or a `review`, always empty on a hard `deny`.** A `forbid` with no review affordance is never softened *or* explained by an annotation. `PolicyEngine` stays unaware of what any annotation value means; it only ever surfaces raw Cedar annotation data — see [ADR 0006](../adr/0006-cedar-policy-stage-and-action-annotations.md) and [ADR 0008](../adr/0008-review-decision-outcome.md). |

## `requires_review` property

```python
@property
def requires_review(self) -> bool:
    return self.effect == REVIEW_ACTION
```

Derived from `effect`, never stored alongside it — two fields that could
disagree is exactly the bug class that would let a review execute without
being caught as one.

## `to_audit_record()`

```python
def to_audit_record(self, *, principal: str, action: str, resource: str, context: Mapping) -> dict:
```

Builds the exact dict every decision-audit sink emits — the structlog
`"decision"` event and the OTel LogRecord both log
`decision.to_audit_record(...)`'s output directly (`**record`), so the
audit trail's shape is identical everywhere a decision is logged. Includes
`principal`, `action`, `resource`, `context` (already stripped of
content-bearing keys by the caller — see [Observability](../OBSERVABILITY.md)),
`decision` (the `effect` string), `reason`, `determining_policies`,
`policy_generation`, `evaluation_ms` (rounded to 3 decimals), and
`annotations` if non-empty.

## What it doesn't carry

- **No request/decision ID.** Correlate via your own logging/tracing IDs
  if you need one.
- **No timestamp.** Timestamping happens in the logging layer (structlog's
  `TimeStamper` processor, or OTel's own record time), not on `Decision`
  itself.
- **No latency for the surrounding model/tool call.** `evaluation_ms` is
  Cedar-only. If you want end-to-end call latency, time it yourself around
  the call — see the quickdemo project's `driver.py` for a worked example
  that reports both side by side.

## Getting a `Decision` out of a call

Three ways, depending on which surface produced it:

```python
# 1. Direct return, when raise_on_deny=False
decision = gov.authorize_tool("get_weather", {}, raise_on_deny=False)
if not decision.allowed:
    ...

# 2. Attached to the raised exception, the default (raise_on_deny=True)
from parapetai_agent import GovernanceDenied
try:
    gov.authorize_tool("get_weather", {})
except GovernanceDenied as exc:
    print(exc.decision.reason, exc.decision.determining_policies)

# 3. The on_decision callback -- fired for EVERY decision, allow or deny,
#    from any of the three integration surfaces, since they all route
#    through the same GovernanceHook.
def my_sink(decision, principal, snapshot, resource, context):
    print(decision.effect, decision.determining_policies, decision.evaluation_ms)

gov = Governor.from_policy_dir("./policies", on_decision=my_sink)
```

`on_decision` is the one truly integration-agnostic capture point — it's
identical whether the call underneath was `Governor`, `GovernedAgent`, or
`GovernedRunner`, which is what makes it the right place to build a
uniform dashboard or audit sink across every framework this SDK supports,
present or future.

See also: [Exceptions](exceptions.md) for `GovernanceDenied`/
`GovernanceReviewRequired`, and [ADR 0008](../adr/0008-review-decision-outcome.md)
for the full reasoning behind `REVIEW` as a third outcome.
