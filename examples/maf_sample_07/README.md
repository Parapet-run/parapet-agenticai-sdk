# maf_sample_07 -- Structured Output (governed, plain OpenAI)

Port of Microsoft Agent Framework's ["OpenAI Chat Client with Structured Output"](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/providers/openai/client_with_structured_output.py)
sample: Pydantic-model structured responses, both non-streaming and
streaming -- now governed by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

## The wiring is the same minimal shape as every `maf_sample_0N/`

```python
def _make_agent() -> GovernedAgent:
    return GovernedAgent(
        client=OpenAIChatCompletionClient(),
        name="CityAgent",
        instructions="You are a helpful agent that describes cities in a structured format.",
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,
    )
```

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why. Unlike the other six
samples in this directory, this one's `.env.example` defaults to plain
OpenAI rather than Azure OpenAI -- see `run_example.py`'s own module
docstring for why: same `OpenAIChatCompletionClient()` class every other
sample uses, just the OTHER auto-detected routing.

## Run

```bash
cp examples/maf_sample_07/.env.example examples/maf_sample_07/.env
# edit .env: fill in OPENAI_API_KEY (or the Azure OpenAI block instead)
uv run --with agent-framework python3 examples/maf_sample_07/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
