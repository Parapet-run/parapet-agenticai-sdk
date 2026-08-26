# maf_sample_05 -- Tool Approval Workflow (governed)

Port of Microsoft Agent Framework's ["Function Tool with Approval"](https://github.com/microsoft/agent-framework/blob/main/python/samples/02-agents/tools/function_tool_with_approval.py)
sample: a tool that requires human approval before it runs -- now
governed by real Cedar policy via
[`parapetai_agent.GovernedAgent`](../../src/parapetai_agent/maf.py).

## The wiring is the same minimal shape as every `maf_sample_0N/`

```python
agent = GovernedAgent(
    client=OpenAIChatCompletionClient(),
    name="WeatherAgent",
    instructions="You are a helpful weather assistant. Use the weather tools to answer.",
    tools=[get_weather, get_weather_detail],
    local_log_dir=EXAMPLE_DIR / "logs",
    console=False,
)
```

No `policy_dir`, no `entities_path`, no manual env-var-trio boilerplate --
see [`maf_sample_01/README.md`](../maf_sample_01/README.md) and
[`docs/maf-integration-pattern.md`](../../docs/maf-integration-pattern.md)
for what each omitted kwarg defaults to and why. Dropped the
streaming-approval variant for brevity; the non-streaming
`handle_approvals` already demonstrates the interesting part.

Worth calling out here specifically: framework-level approval
(`approval_mode="always_require"`) and Cedar's `tool_call` decision are
two independent gates on the same call -- see `run_example.py`'s own
module docstring for why that matters.

## Run

This one is interactive -- `get_weather_detail` prompts `y/n` on stdin
before it runs.

```bash
cp examples/maf_sample_05/.env.example examples/maf_sample_05/.env
# edit .env: fill in AZURE_OPENAI_* or OPENAI_API_KEY
uv run --with agent-framework python3 examples/maf_sample_05/run_example.py
```

## Governed by a real control plane instead

Provision an agent from the control plane's dashboard, copy the
`PARAPETAI_CONTROL_PLANE_URL`/`PARAPETAI_AGENT_SECRET`/`PARAPETAI_AGENT_ID` block from
its agent detail page ("Integrating this agent") into `.env`.
