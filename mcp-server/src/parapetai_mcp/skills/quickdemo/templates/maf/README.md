# Parapet quickdemo — Sales vs HR (Microsoft Agent Framework)

Tony works in Sales. Sally works in HR. Both talk to the same internal
workplace agent, which has two tools available: `salesforce_lookup` (Sales
data) and `hr_lookup` (HR data). Nothing about the agent's code stops Tony
from reading HR data, or Sally from reading Salesforce data — until Parapet
is wired in and a policy says otherwise, based on which org each person is
actually in.

This demo runs the SAME agent both ways and shows the difference:

| File | What it shows |
|---|---|
| `example_no_governance.py` | Plain `agent_framework.Agent`. Both people can call both tools. |
| `example_governed.py` | `GovernedAgent`, same tools, same task. Cedar enforces org-scoped access — Tony gets Salesforce and is denied HR; Sally gets HR and is denied Salesforce. |
| `driver.py` | Runs both, prints a side-by-side allow/deny table, prints the control-plane link for each agent. |

Nothing here needs a real LLM key or a real Salesforce/HR system —
`mock_model_server.py` is a small local stand-in that both examples use by
default, and both tools return canned data. Everything else is real: the
real `agent_framework.Agent`/`GovernedAgent` classes, and (for the governed
example) a real Cedar policy decision from a real Parapet control plane.

## Setup

### 1. Python

You need Python 3.12+.

**macOS**, if you don't already have it:
```
brew install python@3.12
```
Check with `python3 --version`. Any other OS: install from
[python.org](https://python.org) or your package manager.

### 2. A project-local environment + dependencies

Fastest path, using [`uv`](https://docs.astral.sh/uv/) (installs a
project-local `.venv` and resolves `pyproject.toml` in one step):
```
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS/Linux; brew install uv also works
uv sync
```

Without `uv`, plain `venv` + `pip` works the same way:
```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

### 3. A Parapet agent (needed for `example_governed.py` and `driver.py` — `example_no_governance.py` alone needs neither)

If you got this demo from the **parapet-quickdemo** Claude Code skill, this
step already happened — `.env` is already filled in, skip to Run.

Otherwise, by hand:
1. Sign up / log in at your control plane (default: https://app.parapet.run).
2. Install `parapetai-mcp` and log in:
   ```
   pipx install parapetai-mcp        # or: uv tool install parapetai-mcp
   ```
   (No `pipx`? `brew install pipx` on macOS, or
   `python3 -m pip install --user pipx && pipx ensurepath` elsewhere.)
3. In Claude Code (or any MCP client with this server connected), provision
   an agent and push the org policy:
   - `parapet_login`
   - `parapet_provision_agent` (display_name: "quickdemo-maf-governed")
   - `parapet_push_policy_file` with `filename="40-org.cedar"` and the
     contents of `policy/40-org.cedar` in this directory
4. Copy `.env.example` to `.env` and fill in `PARAPETAI_AGENT_ID` /
   `PARAPETAI_AGENT_SECRET` from step 3 (the secret is shown once —
   `parapet_provision_agent`'s response), and `PARAPETAI_ACCOUNT_ID` from
   `parapet_whoami`'s `account_id` field (every agent page on the console
   is scoped under `/a/{account_id}/...`, so this is needed to print a
   working link, not just to run the demo).

### 4. (Optional) a real model instead of the mock

Leave `OPENAI_API_KEY` unset in `.env` and both examples use the built-in
mock model — no key, no network call, fully deterministic. Set a real
`OPENAI_API_KEY` (and `OPENAI_BASE_URL` if not using OpenAI directly) to
use a real model instead; nothing else changes.

## Run

```
uv run python driver.py
```

or run either example alone:
```
uv run python example_no_governance.py
uv run python example_governed.py
```

Expected output (mock mode): the ungoverned run shows all four calls
succeeding; the governed run shows Tony and Sally each denied on the
other's tool. The final lines print each agent's control-plane URL —
open it to see the policy, the allow/deny decisions, and the traces for
yourself.
