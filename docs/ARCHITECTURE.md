# Architecture

Parapet governs an agent from *inside* its process. There is no proxy in the
data path and no network hop on the critical decision — the enforcement runs as
middleware in the same runtime as the model and tool calls it governs.

## The path of one request

```
prompt
  |
  v
[ identity ]        who is calling? (OIDC/JWT roles) — every decision is scoped to a caller
  |
  v
[ input  · pre  ]   PII/secrets/injection scanners + Cedar model_call decision
  |                 DENY -> the model never sees the prompt
  v
[ model call    ]   the prompt goes to the model
  |
  v
[ tool   · call ]   Cedar tool_call authorization, per tool, by name+args+role
  |                 DENY -> the tool never runs; agent continues
  v
[ output · post ]   groundedness (HHEM/lexical) + SLM judge -> Cedar post decision
  |                 DENY -> the answer is withheld
  v
response (only if every gate allowed)
```

Each gate is a Cedar decision. Between gates, only booleans and metadata move —
never the content being judged.

## Why in-process

- **Before the fact.** A tool call is authorized *before* it executes; an
  off-policy answer is caught *before* a token reaches the user. A monitor that
  watches from outside can only react after.
- **Fine-grained.** Deny one action, allow the rest of the turn. Not a
  whole-agent kill switch, not a coarse network block.
- **Content-free egress.** Because the judgment happens where the data already
  is, only the *decision* has to leave. Prompts and responses stay in the
  process.

## Fail-closed by construction

Enforcement never fails open:

- An unparsed payload, an evaluation error, or a missing policy **denies**.
- Cedar is **default-deny**: no matching `permit` is a Deny, and `forbid` always
  beats `permit`.
- A bad or unreachable bundle **keeps the last known-good policy set** — reload
  never empties it.
- Output-eval backends that raise deny rather than pass — a missing verdict can
  never read as "approved".

## The output-eval correlation

Groundedness and the judge are independent scorers over the same response. Each
writes one boolean into the `post` Cedar context (`True` iff it *failed*). A
`@stage("post")` `forbid` gates on those booleans:

```
forbid when groundedness.failed || judge.failed     // OR-to-deny
```

Equivalently: the answer is delivered only when it is *both* grounded *and*
judge-approved (AND-to-approve). Cedar is the combiner — the scorers never talk
to each other.

## The two planes

- **Data plane (this SDK):** in-process enforcement. Runs with or without a
  network. This is the open-source core.
- **Control plane (separate service):** distributes signed policy bundles and
  ingests the content-free decision stream. The SDK is its client — see
  [CONTROL_PLANE_API.md](CONTROL_PLANE_API.md).

The boundary between them is a signed HTTP protocol, and the only thing that
crosses it toward the control plane is governance metadata.
