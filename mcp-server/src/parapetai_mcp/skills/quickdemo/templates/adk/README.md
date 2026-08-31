# Parapet quickdemo — Sales vs HR (Google ADK)

Tony works in Sales. Sally works in HR. Both talk to the same internal
workplace agent, which has two tools available: `salesforce_lookup` (Sales
data) and `hr_lookup` (HR data). Nothing about the agent's code stops Tony
from reading HR data, or Sally from reading Salesforce data — until Parapet
is wired in and a policy says otherwise, based on which org each person is
actually in.

This demo runs the SAME agent both ways and shows the difference:

| File | What it shows |
|---|---|
| `example_no_governance.py` | Plain `google.adk` `Agent` + `Runner`. Both people can call both tools. |
| `example_governed.py` | `GovernedRunner`, same tools, same task. Cedar enforces org-scoped access — Tony gets Salesforce and is denied HR; Sally gets HR and is denied Salesforce. |
| `driver.py` | Runs both, prints a side-by-side allow/deny table, prints the governed agent's control-plane link. |

Nothing here needs a real Gemini key or a real Salesforce/HR system —
`mock_llm.py` is a small local stand-in (a `google.adk.models.BaseLlm`
subclass, no HTTP server needed) that both examples use by default, and
both tools return canned data. Everything else is real: the real
`google.adk` `Agent`/`GovernedRunner` classes, and (for the governed
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
   - `parapet_provision_agent` (display_name: "quickdemo-adk-governed")
   - `parapet_push_policy_file` with `filename="40-org.cedar"` and the
     contents of `policy/40-org.cedar` in this directory
4. Copy `.env.cloud.example` to both `.env` and `.env.cloud`, and fill in
   `PARAPETAI_AGENT_ID` / `PARAPETAI_AGENT_SECRET` from step 3 (the secret
   is shown once — `parapet_provision_agent`'s response), and
   `PARAPETAI_ACCOUNT_ID` from `parapet_whoami`'s `account_id` field.

### 4. (Optional) a real model instead of the mock

Leave `GOOGLE_API_KEY` unset in `.env` and both examples use the built-in
mock model — no key, no network call, fully deterministic. Set a real
`GOOGLE_API_KEY` (from https://aistudio.google.com/apikey) to use a real
Gemini model instead; nothing else changes.

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
other's tool. The final lines print the governed agent's control-plane
URL — open it to see the policy, the allow/deny decisions, and the
traces for yourself. (There's only one agent — `example_no_governance.py`
never calls the control plane, so there's nothing to point at there.)

## Reading the decision data

Each governed row in `driver.py`'s table carries four extra columns beyond
ALLOWED/DENIED:

- **policy** — cedarpy's own determining-policy id(s) (its
  `diagnostics.reasons`, e.g. `policy3`) for whichever rule in
  `policies/00-base.cedar`/`40-org.cedar` decided the outcome. Which rule
  denied Sally's `salesforce_lookup` call, not just that something did.
  Note this is cedarpy's raw positional id (assigned in file-concatenation
  order across every loaded `.cedar` file), not the friendlier `@id(...)`
  annotation you see in the `.cedar` source (e.g.
  `salesforce_requires_sales_org`) — `parapetai_agent`'s own resolution of
  `@id` to a readable label (`policy/engine.py`'s `_policy_labels()`) is
  private and only used to build `Decision.reason` for a `review`-eligible
  denial, not for a plain hard deny like this demo's org policy. For a
  hard deny, `Decision.reason` itself is a generic `"denied: no permit
  matched or forbid applied"` — cross-reference the printed `policy3`-style
  id against the `.cedar` files' statement order by hand if you need the
  name.
- **cedar eval** — how long that one Cedar evaluation took, in
  milliseconds (wall-clock around `cedarpy.is_authorized()`, not the
  whole tool/model call).
- **total** — wall-clock latency for the whole scenario (model call + tool
  call + governance checks), timed by this demo around `runner.run_async()`,
  not something `Decision` itself carries.
- **tokens** — `total_token_count` from the model response's own usage
  data (`Event.usage_metadata`, a `google.genai` field, nothing to do with
  Cedar). `$0.00` in mock mode (canned figures from `mock_llm.py`);
  real-model runs print the token count only — this SDK has no per-model
  pricing table, so no dollar estimate.

`policy` and `cedar eval` come straight off `parapetai_agent.policy.engine.
Decision` — the same dataclass every integration this SDK ships
(framework-neutral `Governor`, `GovernedRunner` here, `GovernedAgent` in
the MAF version of this demo, and any future one) produces from the exact
same `PolicyEngine.evaluate()` call. Nothing about
`determining_policies`/`evaluation_ms` is ADK-specific — only how a DENY
gets folded back into the agent's own response differs per framework (a
synthetic `LlmResponse`/tool-result dict here, never an exception — see
`example_governed.py`'s own comments), because that part is constrained by
whatever extension point each framework offers. The decision data itself
is generic; `example_governed.py` pulls it out of the same structlog
`"decision"` event every framework emits (`_capture` in that file), not
from anything framework-specific. `total` and `tokens` are NOT part of
`Decision` at all — this demo adds them on top from the framework's own
response object, to show governance overhead and usage side by side with
the Cedar outcome.

## Switching between cloud and local mode

`example_governed.py` reads `PARAPETAI_MODE` from `.env` to decide where
Cedar policy comes from:

| `PARAPETAI_MODE` | Policy source | Needs a control plane? |
|---|---|---|
| `cloud` (default) | The real bundle on your Parapet control plane, fetched at startup | Yes — `PARAPETAI_AGENT_ID`/`_SECRET`/`_ACCOUNT_ID` |
| `local` | `./policies/*.cedar` on disk, read directly, no network call | No |

Two ready-made env files make swapping one command instead of hand-editing:

```
cp .env.local .env    # switch to local mode -- edit ./policies/, iterate fast
uv run python driver.py

cp .env.cloud .env    # switch back to cloud mode -- against the real agent
uv run python driver.py
```

`.env.cloud` is a backup of your real, filled-in credentials (identical to
what `.env` started as) — never edit it by hand, and never edit `.env`
directly while meaning to change cloud-mode settings; edit `.env.cloud`
and re-copy it instead, so a stray edit while testing locally can't get
lost. `.env.local` has no secret in it at all (local mode never talks to
the control plane), so it's safe to keep, share, or check in.

`./policies/` always has two files: `00-base.cedar` (a base `permit` on
model_call/tool_call/http_request -- CEDAR IS DEFAULT-DENY, so without
this every tool_call denies outright, not because of the org policy;
mirrors both a freshly provisioned agent's starter bundle and
`parapetai_agent`'s own bundled default policy) and `40-org.cedar` (the
exact same rule that was pushed to the control-plane bundle). Both are
kept in sync on purpose so switching `PARAPETAI_MODE` doesn't change what's
enforced, only where it's enforced from. Add more `.cedar` files to that
directory to test additional rules without touching the control plane at
all; the quickdemo skill can seed a
few ready-made ones on generation (deny a tool outright, require an extra
identity claim) — see `../templates/policy_library/quickdemo/` in the SDK
repo this project was generated from, or just write your own.

## Deployment

Nothing in this demo is meant to be deployed as-is — it's a local
teaching tool. If you do turn it into something you ship, watch two
things:

- **`.parapet-cache/`** (`PARAPETAI_PERSIST_POLICY_DIR` in `.env.cloud`,
  on by default) is a disposable local debug dump of whatever bundle the
  control plane last sent this machine — never a source of truth, and
  stale the moment the real bundle changes. It's already in `.gitignore`;
  make sure any build/deploy step (Docker build context, zip, CI artifact)
  excludes it too, the same way you'd exclude `.env`/`.env.cloud`. If it
  ends up inside a deployed image, at best it's dead weight; at worst
  someone reads it as current policy when it isn't.
- **`.env` / `.env.cloud`** carry a real agent secret once filled in —
  never commit them (already gitignored) and never bake them into an
  image; inject `PARAPETAI_AGENT_SECRET` at deploy time the way you would
  any other credential.
