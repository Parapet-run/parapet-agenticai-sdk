# maf_sample_06 -- Function-based Middleware (governed)

Port of Microsoft Agent Framework's ["Function-based Middleware"](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/middleware/function_based_middleware.py)
sample: framework-native security and logging middleware, implemented as
plain async functions -- now layered alongside real Cedar governance via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

Uses the same `FoundryChatClient` + async `azure.identity.aio.AzureCliCredential`
upstream does (this specific sample uses the async credential variant as
an `async with` context manager, unlike `maf_sample_01/02/03/04`'s sync
one) -- see `run_example.py`'s own module docstring and
[`conformance/matrix.yaml`](../../conformance/matrix.yaml)'s `foundry` entry.

## The wiring is the same minimal shape as every `maf_sample_0N/`

```python
async with (
    AzureCliCredential() as credential,
    GovernedAgent(
        client=FoundryChatClient(credential=credential),
        name="WeatherAgent",
        instructions="You are a helpful weather assistant.",
        tools=[get_weather],
        middleware=[security_agent_middleware, logging_function_middleware],
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,
    ) as agent,
):
    ...
```

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why.

Worth calling out here specifically: `middleware=[...]` passed to
`GovernedAgent` runs AFTER Cedar's own governance middleware, never
instead of it -- see `run_example.py`'s own module docstring for why
that ordering matters.

## Run

```bash
az login   # AzureCliCredential -- once, not settable via .env
cp examples/maf_sample_06/.env.example examples/maf_sample_06/.env
# edit .env: fill in FOUNDRY_PROJECT_ENDPOINT
uv run --with agent-framework python3 examples/maf_sample_06/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
