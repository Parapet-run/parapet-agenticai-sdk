# maf_sample_02 -- Add Tools (governed)

Port of Microsoft Agent Framework's ["Add Tools"](https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/02_add_tools.py)
sample: a `@tool`-decorated function wired into an agent -- now governed
by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

Uses the same `FoundryChatClient` + `AzureCliCredential` upstream does --
see `run_example.py`'s own module docstring and
[`conformance/matrix.yaml`](../../conformance/matrix.yaml)'s `foundry` entry.

## The wiring is the same minimal shape as every `maf_sample_0N/`

```python
agent = GovernedAgent(
    client=FoundryChatClient(credential=AzureCliCredential()),
    name="WeatherAgent",
    instructions="...",
    tools=[get_weather],
    local_log_dir=EXAMPLE_DIR / "logs",
    console=False,
)
```

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why. Worth calling out here
specifically: the tool call itself (`get_weather`) is now a real Cedar
`tool_call` decision, not just a framework-level function invocation --
see `run_example.py`'s own module docstring.

## Run

```bash
az login   # AzureCliCredential -- once, not settable via .env
cp examples/maf_sample_02/.env.example examples/maf_sample_02/.env
# edit .env: fill in FOUNDRY_PROJECT_ENDPOINT
uv run --with agent-framework python3 examples/maf_sample_02/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
