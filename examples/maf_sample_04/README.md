# maf_sample_04 -- Agent Memory / Context Providers (governed)

Port of Microsoft Agent Framework's ["Agent Memory with Context Providers and Session State"](https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/04_memory.py)
sample: a `ContextProvider` that remembers the user's name in session
state across turns -- now governed by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

Uses the same `FoundryChatClient` + `AzureCliCredential` upstream does --
see `run_example.py`'s own module docstring and
[`conformance/matrix.yaml`](../../conformance/matrix.yaml)'s `foundry` entry.

## The wiring is the same minimal shape as every `maf_sample_0N/`

```python
agent = GovernedAgent(
    client=FoundryChatClient(credential=AzureCliCredential()),
    name="MemoryAgent",
    instructions="You are a friendly assistant.",
    context_providers=[UserMemoryProvider()],
    local_log_dir=EXAMPLE_DIR / "logs",
    console=False,
)
```

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why. Worth calling out here
specifically: `context_providers=[...]` passes straight through
`GovernedAgent`'s `**kwargs` to `agent_framework.Agent` unchanged -- see
`run_example.py`'s own module docstring.

## Run

```bash
az login   # AzureCliCredential -- once, not settable via .env
cp examples/maf_sample_04/.env.example examples/maf_sample_04/.env
# edit .env: fill in FOUNDRY_PROJECT_ENDPOINT
uv run --with agent-framework python3 examples/maf_sample_04/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
