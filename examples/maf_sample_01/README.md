# maf_sample_01 -- Hello Agent (governed)

Port of Microsoft Agent Framework's ["Hello Agent"](https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/01_hello_agent.py)
sample: the simplest possible agent, run in both non-streaming and
streaming modes -- now governed by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

Uses the same `FoundryChatClient` + `AzureCliCredential` upstream does --
see `run_example.py`'s own module docstring for confirmation that
`agent-framework-foundry` is a thin wrapper around `agent-framework-core`
(its `FoundryChatClient` literally subclasses `agent-framework-openai`'s
client under the hood) and
[`conformance/matrix.yaml`](../../conformance/matrix.yaml)'s `foundry` entry
for exactly what's been verified.

## The wiring is now genuinely minimal

```python
from parapetai_agent import GovernedAgent  # was: from agent_framework import Agent

agent = GovernedAgent(
    client=FoundryChatClient(credential=AzureCliCredential()),
    name="HelloAgent",
    instructions="You are a friendly assistant. Keep your answers brief.",
)
```

That's the entire integration -- no `policy_dir`, no `entities_path`, no
manual `REPO_ROOT`/env-var-trio boilerplate. See
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each optional kwarg does and why omitting it is safe:

- **`policy_dir`/`entities_path`** (omitted): enforces the Cedar policy
  set bundled inside `parapetai-agent` (co-located with `PolicyEngine`, the
  class that loads it -- base permits) -- a real, installed, read-only
  package file, not a required setup step, and never needs a writable
  filesystem.
- **`agent_id`/`control_plane_url`/`agent_secret`** (omitted): fall back
  to `PARAPETAI_AGENT_ID`/`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET` in
  `.env`.
- **`persist_policy_dir`** (omitted): a control-plane-pulled bundle
  stays in memory only, never written to disk -- the right default for
  a serverless/k8s process with no writable volume.
- **`local_log_dir`** (set, in this sample): opt-in rotating audit log,
  one kwarg instead of a separate `configure_rotating_audit_log()` call.

Also demonstrates `governed_identity(credential=...)` -- one context
manager for every identity source `parapetai_agent` knows how to read
(`claims=`/`roles=` already-parsed, `token=` a raw bearer JWT, or
`credential=` an azure-identity credential, dispatching internally to
`current_identity()`/`identity_from_bearer_token()`/
`identity_from_azure_credential()` so the caller never has to pick which
one matches their data's shape) -- extracting the signed-in `az login`
identity from the SAME `AzureCliCredential` already passed to
`FoundryChatClient`, for Cedar policies that need to know WHO is signed
in, not just WHICH agent is calling -- and the seam for swapping the
default in-memory identity/session store
(`parapetai_agent.configure_identity_store()`) for a real shared backend,
shown as a commented-out example since this single-script sample has no
actual multi-replica need for one.

## Run

```bash
az login   # AzureCliCredential -- once, not settable via .env
cp examples/maf_sample_01/.env.example examples/maf_sample_01/.env
# edit .env: fill in FOUNDRY_PROJECT_ENDPOINT
uv run --with agent-framework python3 examples/maf_sample_01/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, then copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block its
agent detail page prints ("Integrating this agent") into `.env`. This
script then pulls that agent's real policy bundle instead of the
bundled default, and pushes decision audit events back to the control
plane.
