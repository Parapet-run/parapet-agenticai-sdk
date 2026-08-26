# adk_sample_01 -- Hello Agent (governed)

Google ADK's own ["Quickstart"](https://google.github.io/adk-docs/get-started/quickstart/)
shape -- the simplest possible agent, run in both non-streaming and
streaming modes -- now governed by real Cedar policy via
[`parapetai_agent.adk.GovernedRunner`](../../src/parapetai_agent/adk.py).

The first `parapetai-agent[adk]` example in this directory; see
[`maf_sample_01/`](../maf_sample_01/README.md) for the Microsoft Agent
Framework equivalent this one is deliberately shaped after (same
minimal-example structure, different framework, different governable
seam).

## The wiring is now genuinely minimal

```python
from parapetai_agent.adk import GovernedRunner  # was: from google.adk.runners import Runner

runner = GovernedRunner(
    agent=root_agent,
    app_name="hello-agent",
    session_service=InMemorySessionService(),
)
```

That's the entire integration -- no `policy_dir`, no `entities_path`, no
manual `REPO_ROOT`/env-var-trio boilerplate. `GovernedRunner` is a
drop-in `google.adk.runners.Runner` replacement the same way
`GovernedAgent` is a drop-in `agent_framework.Agent` one -- the class it
wraps differs (`Runner`, not `Agent`) because ADK's own architecture puts
the governable seam there (a `Runner(plugins=[...])` `BasePlugin`, not
per-`Agent` middleware) -- see `adk.py`'s own module docstring for why
that's a real, verified difference between the two frameworks, not an
inconsistency in this SDK.

- **`policy_dir`/`entities_path`** (omitted): enforces the Cedar policy
  set bundled inside `parapetai-agent` (co-located with `PolicyEngine`,
  the class that loads it -- base permits) -- a real, installed,
  read-only package file, not a required setup step, and never needs a
  writable filesystem.
- **`agent_id`/`control_plane_url`/`agent_secret`** (omitted): fall back
  to `PARAPETAI_AGENT_ID`/`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`
  in `.env`.
- **`local_log_dir`** (set, in this sample): opt-in rotating audit log,
  one kwarg instead of a separate `configure_rotating_audit_log()` call.
- **`console=False`** (set, in this sample): `local_log_dir` still writes
  the file; this just skips also echoing every decision as a JSON line to
  stdout, so it doesn't interleave with this script's own
  `print(f"Agent: ...")` output.

## Identity, two ways

The script demonstrates both identity sources `parapetai_agent.adk` reads,
same precedence as every other in-process integration
(`parapetai_agent.scoped_data`, shared with `parapetai_agent.maf`):

1. **ADK's own `Session.user_id`** (`"alice"`, passed to
   `runner.run_async(user_id="alice", ...)`) -- **OFF by default**
   (`GovernedRunner(trust_session_user_id=False)`), because `user_id` is
   unverified and ADK requires it on every call, unlike MAF where identity
   is fully optional -- defaulting it on would make identity-gated Cedar
   policies (e.g. `policies/30-identity.cedar`) silently stricter for ADK
   than for MAF on the same bundle (a real finding, not a hypothetical --
   see `parapetai_agent/adk.py`'s own module docstring's "Identity"
   section). This script opts in explicitly
   (`trust_session_user_id=True`) since it's a single-operator CLI sample
   with no real caller to protect against -- see
   [`adk_webapp/`](../adk_webapp/README.md) for the shape where that
   distinction actually matters. With it on, the non-streaming call below
   gets `identity_claims: {"sub": "alice"}` in the audit log, no extra
   import needed.
2. **`governed_identity()`** -- the SAME context manager
   `parapetai_agent.maf` exports, unchanged: pick `claims=`/`roles=`
   (already parsed) or `token=` (a raw bearer JWT), dispatching
   internally so callers never have to pick which mechanism matches
   their data's shape. Wrapping the streaming call below asserts a real
   `oid`/role set that OVERRIDES the session's own `user_id` for the
   duration of the block -- the same explicit-wins-ambient-fallback
   precedence documented in `scoped_data.effective_identity_claims()`.
   This is the one that actually matters for a real deployment: it's the
   only source here that can carry a VERIFIED identity.

## Run

```bash
cp examples/adk_sample_01/.env.example examples/adk_sample_01/.env
# edit .env: fill in GOOGLE_API_KEY (free tier: https://aistudio.google.com/apikey)
uv run --extra adk python3 examples/adk_sample_01/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, then copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID`
block its agent detail page prints ("Integrating this agent") into `.env`.
This script then pulls that agent's real policy bundle instead of the
bundled default, and pushes decision audit events back to the control
plane.
