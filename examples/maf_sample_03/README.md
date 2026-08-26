# maf_sample_03 -- Multi-Turn Conversations (governed)

Port of Microsoft Agent Framework's ["Multi-Turn Conversations"](https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/03_multi_turn.py)
sample: reusing an `AgentSession` object to keep conversation history
across calls -- now governed by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

Uses the same `FoundryChatClient` + `AzureCliCredential` upstream does --
see `run_example.py`'s own module docstring and
[`conformance/matrix.yaml`](../../conformance/matrix.yaml)'s `foundry` entry.

## The wiring is the same minimal shape as every `maf_sample_0N/`

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why. Worth calling out here:
each turn on the same session is still an independent Cedar decision --
see `run_example.py`'s own module docstring.

## Run

```bash
az login   # AzureCliCredential -- once, not settable via .env
cp examples/maf_sample_03/.env.example examples/maf_sample_03/.env
# edit .env: fill in FOUNDRY_PROJECT_ENDPOINT
uv run --with agent-framework python3 examples/maf_sample_03/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
