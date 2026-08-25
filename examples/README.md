# Examples

## `authorize_tool_calls.py` — runs on the base install

Authorizes tool calls against the example Cedar bundle in `../policies` — no
model, no network, no control plane. Shows allow vs deny and the determining
policy.

```bash
pip install parapetai-agent
python examples/authorize_tool_calls.py
```

## Governing a real agent

Wrap a Microsoft Agent Framework agent (needs the `maf` extra and a model
endpoint), and every model and tool call becomes a governed decision:

```python
from parapetai_agent import GovernedAgent as Agent, GovernanceDenied

agent = Agent(
    name="support",
    instructions="Help the customer.",
    tools=[lookup_order],
    policy_dir="./policies",        # or control_plane_url=... for a signed bundle
)

try:
    result = await agent.run("Where is order A1001?")
except GovernanceDenied as denied:
    print("blocked:", denied.reason)
```

See the top-level `README.md` for identity binding, the control-plane API, and
OTel export.
