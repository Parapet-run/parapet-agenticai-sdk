---
name: parapet-langgraph
description: Use when the user asks to govern a LangGraph/LangChain (langchain.agents.create_agent) agent with Parapet, add Parapet to a project that already uses langchain/langgraph, provision a Parapet agent for a LangGraph project, or wire up Parapet/parapetai-agent[langgraph] env vars. Requires the parapet MCP server (parapetai-mcp) to be connected. For a Microsoft Agent Framework (agent_framework) project use the parapet-maf skill instead, and for Google ADK (google-adk) use parapet-adk — all three instrumentation procedures are genuinely different, not just naming differences.
---

# Parapet + LangGraph / LangChain: provision + instrument

This skill drives the `parapet_*` MCP tools to authenticate, provision a
governed agent, and instrument a LangGraph/LangChain (`langchain.agents`)
codebase to route through it. The tools only talk to the control plane —
**you** make the actual file edits, with your normal file tools, following
the steps below.

If the target project uses Microsoft Agent Framework (`agent_framework`)
instead — check for `from agent_framework...`/`import agent_framework` —
use the **parapet-maf** skill. If it uses Google ADK (`google.adk`), use
**parapet-adk**. The three frameworks put governance in genuinely different
places (see step 4 below), so applying this skill's instructions to a MAF
or ADK project, or vice versa, will not work.

## 1. Check auth

Call `parapet_whoami`. All `parapet_*` tools default to the hosted control
plane at `https://app.parapet.run` unless the MCP server was registered with
a `PARAPETAI_CONTROL_PLANE_URL` override (e.g. a local `make dev` instance) —
don't pass a different `control_plane_url` from memory or guesswork.

If `parapet_whoami` returns an `error` (not logged in):

1. Call `parapet_login_start`. It returns immediately with a
   `verification_uri_complete` (one click, code pre-filled — it also tries
   opening this in the user's browser itself), a bare `verification_uri`,
   and a `user_code`. **Show the user both** — the browser call succeeding
   doesn't mean they saw a tab open, and they may want to approve from a
   different device (e.g. type the short code on their phone instead of a
   long URL). They need to already be signed in to the control plane's
   dashboard, or the page will ask them to.
2. Call `parapet_login_wait` with the `device_code` and `expires_in` from
   step 1 (as `timeout_seconds`). It polls until approved and returns (or
   times out after ~10 minutes — if it times out, call `parapet_login_start`
   again).
3. Re-run `parapet_whoami` to confirm.

Never ask the user to paste a token, an agent secret, or any other
credential into the chat. This flow never requires that.

## 2. Provision the agent

Call `parapet_provision_agent` (optionally with `display_name` set to
something recognizable, e.g. the project's directory name). It returns
`{agent_id, secret}` — **the secret is shown exactly once and cannot be
retrieved again.** Do not just print it in chat and move on; write it
straight into the target project's config in the next step. Never repeat
the secret back to the user after that write — treat it the same way you'd
treat any other credential you just persisted to disk.

## 3. Get the deployment's own config

Call `parapet_get_quickstart`. As of this writing it only returns fields
for the MAF/ADK integrations (`sdk_install`/`default_model`,
`sdk_install_adk`/`default_model_adk`) — there is no `sdk_install_langgraph`/
`default_model_langgraph` yet (verify this is still true by checking the
tool's actual response shape, not this note, before assuming). Until the
control plane adds those:

- Install command: `parapetai-agent[langgraph]` (plus whatever the
  project's own chat model integration package already is, e.g.
  `langchain-openai` — do not add a new one it doesn't already depend on).
- Model id: use whatever chat model the target project's existing code
  already constructs — don't invent or hardcode one from memory.
- `python_min` from `parapet_get_quickstart` still applies (one Python
  floor for the whole `parapetai-agent` package, regardless of extra) —
  that field is not framework-specific.

## 4. Wire the target project

LangGraph's governable seam is `langchain.agents.create_agent`'s own
`middleware: Sequence[AgentMiddleware] = ()` parameter — **not** the older,
deprecated `langgraph.prebuilt.create_react_agent`, which predates
`middleware=` support entirely and cannot block a call before it executes
(verify which one the target project uses with a grep before assuming;
see "If the project uses `create_react_agent`" below if it does).

```python
from langchain.agents import create_agent
from parapetai_agent.langgraph import build_middleware

agent = create_agent(
    model,
    tools=[...],
    middleware=[build_middleware(agent_id="...", agent_secret="...", control_plane_url="...")],
)
```

Construct `build_middleware(...)` with `agent_id=`, `agent_secret=`,
`control_plane_url=` from steps 2–3. There is no wrapper class here the way
MAF has `GovernedAgent`/ADK has `GovernedRunner` — `build_middleware()`
itself is the only public entry point; do not look for a
`GovernedAgent`/`GovernedGraph` class in `parapetai_agent.langgraph`, it
does not exist.

### Coverage is per-construction-site, not automatic — read this before declaring done

Like the MAF skill's model, unlike the ADK skill's: there is no LangGraph
equivalent of "wrap the Runner, every agent underneath it is governed."
Each `create_agent(...)` call is independently middleware-configured. A
multi-agent LangGraph project (a supervisor graph, DeepAgents'
`create_deep_agent`, or any hand-rolled graph wiring one agent's output into
another) needs **every** `create_agent`/`create_deep_agent` call site given
its own `middleware=[build_middleware(...)]` — wiring the top-level/
supervisor agent does **not** govern the agents it delegates to.

Concretely:

1. **Find every construction site, not just one.** Grep the whole target
   codebase for `create_agent(` and `create_deep_agent(` (and
   `create_react_agent(` — see below). A single file adding
   `middleware=[build_middleware(...)]` to its own `create_agent(...)` call
   governs only *that* call; it does **not** govern a `create_agent(...)`
   call in any other file or graph node.
2. **Verify, don't just edit.** After wiring, grep again for `create_agent(`
   and `create_deep_agent(` across the whole codebase and confirm every
   match has a `middleware=` list containing a `build_middleware(...)` (or
   already-constructed `ParapetAgentMiddleware`) instance. Anything without
   one is an ungoverned construction site — fix it before declaring this
   step done.
3. **This is a point-in-time transformation, not an ongoing guarantee.** If
   the developer adds a *new* `create_agent(...)`/`create_deep_agent(...)`
   call later without following the same convention, it is silently
   ungoverned. Tell the user this explicitly once instrumentation is done —
   either re-run this skill when new agent construction is added, or
   establish a project convention (lint rule, code review checklist)
   banning a bare `create_agent(...)` with no `middleware=` entry from this
   package.

### If the project uses `create_react_agent`

`langgraph.prebuilt.create_react_agent` has no `middleware=` parameter at
all — there is no way to attach `build_middleware()`'s `AgentMiddleware` to
it. Two options, and the choice is the project's to make, not yours to
assume:

- **Migrate the call site to `langchain.agents.create_agent`** (the
  current, non-deprecated construction API, confirmed against
  `langchain>=1.3`/`langgraph>=1.2`) if the project can take the full
  `langchain` package as a dependency (not just `langgraph`/
  `langchain-core`) — then follow step 4 above unchanged.
- **Use the older, tool-call-only `Governor.tool` path instead** if the
  project needs to keep `create_react_agent` (e.g. to stay on the lighter
  `langchain-core`-only footprint):

  ```python
  from langchain_core.tools import tool as lc_tool
  from langgraph.prebuilt import create_react_agent
  from parapetai_agent import Governor

  gov = Governor.from_policy_dir("./policies")  # or from_control_plane(...)

  @gov.tool
  def lookup_order(order_id: str) -> str: ...

  agent = create_react_agent(model, tools=[lc_tool(lookup_order)])
  ```

  Tell the user explicitly this path is tool-call-only — no pre/post-model
  Cedar gating, no ambient identity via `governed_identity()` — it is not
  equivalent coverage to `AgentMiddleware`, just the best available for a
  project that can't move off `create_react_agent`.

### Known gaps — do not overclaim parity with the MAF/ADK skills

This integration is real and tested for its three governed stages
(pre-model, tool-call, post-model — same three MAF/ADK cover), but as of
this SDK version it does NOT yet have:

- Tier-2 content-checks/groundedness/judge scanning, or ALTER support
  (`build_middleware()` has no `alter_transforms=` parameter at all —
  don't pass one).
- A per-call OTel span with OpenInference attributes (decisions still reach
  OTel as LogRecords via the same audit sink MAF/ADK use — only the
  additional per-call span is missing, not decision observability).
- Verified streaming support (`.astream()`/`.stream()` has not been
  confirmed against a live call — say so if asked, don't assert it works).

Verify against `parapetai_agent/langgraph.py`'s own module docstring for
the current, authoritative list before telling a user what is/isn't
supported — this section can drift from a specific installed version.

- Before calling this step done, actually construct the agent (or at
  minimum import the wired module and run `inspect.signature(...)` on
  `build_middleware`) rather than stopping at static file edits — a wrong
  kwarg name won't show up from editing alone.

## Base-URL interception (gateway) — the framework-agnostic alternative

For any OpenAI-SDK-shaped client, regardless of framework: set
`OPENAI_BASE_URL` to the gateway's URL and forward the agent's
`agent_id`/`agent_secret` however that project already manages secrets
(env file, secret manager — match its existing convention, don't invent a
new one). This is a real alternative to the in-process `build_middleware()`
wiring above, not a fallback for when it doesn't apply — ask which fits the
project if it isn't obvious.

Either way:

- Look for an existing `.env`/`.env.local` first. **Ask before
  overwriting** any existing Parapet-related values in it — don't silently
  clobber a developer's prior setup.
- Use the exact env var names and install command from
  `parapet_get_quickstart` where it has them (see step 3's caveat above for
  the fields it doesn't yet have for this framework).

## Non-negotiables

- Never print the agent secret, the cli token, or the contents of
  `~/.parapet/credentials.json` into chat once written to disk.
- Never suggest disabling fail-closed defaults, weakening a policy bundle,
  or switching credential mode to make something "just work" — if a
  request seems denied, say so and suggest checking the control plane
  dashboard, don't work around it.
- If `parapet_provision_agent` returns a permission error, tell the user
  their account role doesn't allow provisioning (viewer role) — don't
  retry, don't try another endpoint.
