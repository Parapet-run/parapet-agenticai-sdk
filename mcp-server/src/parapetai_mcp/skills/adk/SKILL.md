---
name: parapet-adk
description: Use when the user asks to govern a Google ADK (google-adk) agent with Parapet, add Parapet to a project that already uses google.adk, provision a Parapet agent for an ADK project, or wire up Parapet/parapetai-agent[adk] env vars. Requires the parapet MCP server (parapetai-mcp) to be connected. For a Microsoft Agent Framework (agent_framework) project, use the parapet-maf skill instead — the instrumentation procedure is genuinely different, not just a naming difference.
---

# Parapet + Google ADK: provision + instrument

This skill drives the `parapet_*` MCP tools to authenticate, provision a
governed agent, and instrument a Google ADK (`google-adk`) codebase to
route through it. The tools only talk to the control plane — **you** make
the actual file edits, with your normal file tools, following the steps
below.

If the target project uses Microsoft Agent Framework (`agent_framework`)
instead — check for `from agent_framework...`/`import agent_framework`
before assuming this is the right skill — use the **parapet-maf** skill
instead. The two frameworks put governance in genuinely different places
(see step 4 below), so applying this skill's instructions to a MAF
project, or vice versa, will not work.

## 1. Check auth

Call `parapet_whoami`. All `parapet_*` tools default to the hosted control
plane at `https://app.parapet.run` unless the MCP server was registered with
a `PARAPETAI_CONTROL_PLANE_URL` override (e.g. a local `make dev` instance) —
don't pass a different `control_plane_url` from memory or guesswork.

If `parapet_whoami` returns an `error` (not logged in):

1. Call `parapet_login`. It prints a URL and a short code.
2. Tell the user to open that URL and approve the login in their browser —
   they need to already be signed in to the control plane's dashboard, or
   the page will ask them to.
3. `parapet_login` polls internally and returns once approved (or times
   out after ~10 minutes — if it times out, just call it again).
4. Re-run `parapet_whoami` to confirm.

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

Call `parapet_get_quickstart`. It returns this control plane deployment's
actual config for BOTH framework integrations, not just this one — use the
ADK-named fields specifically: `sdk_install_adk` (not `sdk_install`, that's
the MAF one) and `default_model_adk` (not `default_model`, an OpenAI model
id — meaningless to `google-adk`'s Gemini client). `python_min` is shared
(one Python floor for the whole `parapetai-agent` package, regardless of
extra). Always use these values, never hardcode a `pip install` string,
model id, or env var name from memory, since a different deployment can
configure all of them differently.

## 4. Wire the target project

ADK's governable seam is the `Runner`, not any individual `Agent`/
`LlmAgent` — a `Runner(plugins=[...])` `BasePlugin` registration, applied
**once**, governs every agent it reaches: the root agent, every
`sub_agents=[...]` entry, and every step of a `SequentialAgent`/
`ParallelAgent`/`LoopAgent` workflow, transitively. Verified against
`google-adk`'s own source, not assumed: `BaseAgent._create_invocation_context()`
derives a child agent's context via `parent_context.model_copy(update={'agent':
self})` — a shallow copy that reuses the exact same `plugin_manager`
object from the parent, for every agent type including the workflow ones.
This means step 4 here is a **structurally different, and structurally
smaller, task than the MAF skill's**: find the `Runner` construction
site(s), not every `Agent(...)` construction site.

First, work out which of two shapes the target project uses — they need
different fixes:

### Shape A: the project constructs its own `Runner`/`InMemoryRunner`

The common case for a hand-rolled script or web app. Swap the import:

```python
from parapetai_agent.adk import GovernedRunner as Runner
# or, if the project uses google.adk.runners.InMemoryRunner specifically:
from parapetai_agent.adk import InMemoryGovernedRunner as InMemoryRunner
```

Alias it exactly as shown, matching whichever class the project already
imports (`Runner` or `InMemoryRunner`) — check which one with a grep
before choosing; they take different constructor defaults
(`InMemoryGovernedRunner` pre-fills in-memory session/artifact/memory
services and defaults `app_name`, `GovernedRunner` requires
`session_service=` explicitly, same as bare `Runner` does). Construct it
with `agent_id=`, `agent_secret=`, `control_plane_url=` from steps 2–3,
same kwargs either class accepts.

If the project passes `plugins=[...]` directly to `Runner(...)`, or
`app=App(..., plugins=[...])`, leave that alone — `GovernedRunner`
appends its own governance plugin alongside whatever's already there
(confirmed: passing both `app=` and `plugins=` to bare `Runner` raises
`ValueError` as of google-adk 2.7, since `plugins=` is deprecated in
favor of `App(plugins=[...])` — `GovernedRunner` already branches on
which one the caller used, so just swap the class, don't restructure the
call site to work around this).

**If the project constructs more than one independent `Runner`** (rare,
but check for it — grep for `Runner(` and `InMemoryRunner(` project-wide,
not just near the entry point), each one needs its own swap. Unlike a
single Runner's own agent tree (governed automatically, see above),
separate `Runner` instances do not share a plugin registration.

### Shape B: the project uses `adk web` / `adk api_server` / `get_fast_api_app()`

ADK's own CLI/web-server tooling constructs its `Runner` **internally**
(`api_server.py`'s `_create_runner()`, hardcoded to build a plain
`Runner(app=..., ...)` per discovered agent) — there is no `Runner(...)`
call site in the target project to swap in this shape at all. Do not try
to find one.

Instead, use ADK's own `extra_plugins` mechanism, which this internal
construction path already threads a caller-supplied plugin list through.
Create a small module the target project doesn't already have, exporting
an **already-constructed** plugin instance (not the class — see why
below):

```python
# e.g. parapet_plugin.py, next to the project's agents_dir
from parapetai_agent.adk import build_plugin

parapet_plugin = build_plugin(agent_id="...", agent_secret="...", control_plane_url="...")
```

Then pass its fully-qualified dotted name as an `extra_plugins` entry —
via the CLI (`adk web --extra_plugins parapet_plugin.parapet_plugin
path/to/agents_dir`, matching however the project already invokes `adk
web`/`adk api_server`) or the `get_fast_api_app(extra_plugins=[...])`
kwarg if the project calls that directly. Verified against
`api_server.py`'s own `_instantiate_extra_plugins()`/`_import_plugin_object()`:
the qualified name is resolved via `module_name, obj_name =
qualified_name.rsplit(".", 1); getattr(importlib.import_module(module_name),
obj_name)`, and MUST already be a `BasePlugin` **instance** — if it
resolves to a class instead, ADK tries to instantiate it as
`plugin_obj(name=qualified_name)`, passing only `name=`, which does not
match `ParapetPlugin.__init__`'s required `engine`/`caller` positional
arguments and will fail. `build_plugin()`'s return value is already the
right shape; do not pass `parapetai_agent.adk.ParapetPlugin` itself as
the qualified name.

### Identity, for either shape, if the project is web-deployed

`google.adk.cli.fast_api.get_fast_api_app()` returns a real `FastAPI`
(built on Starlette) either way, so
`parapetai_agent.identity_middleware.IdentityMiddleware` works
unchanged — add it once:

```python
from parapetai_agent.identity_middleware import IdentityMiddleware, jwt_bearer_extractor
app.add_middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())
```

Verified live, not assumed: ADK's own `Session.user_id` (what
`runner.run_async(user_id=..., ...)` takes) is a plain, unauthenticated
string — ADK never verifies it, and `adk web`'s own REST endpoints are
unauthenticated by design (its CLI docstring says so explicitly). If the
project has any identity-gated Cedar policy, `IdentityMiddleware` (or an
equivalent auth layer feeding `governed_identity()`) is the only place a
*verified* identity can come from for a web deployment. Don't turn on
`trust_session_user_id=True` on `GovernedRunner`/`build_plugin` to work
around this instead — that folds the same unverified `user_id` into Cedar
context directly, which is appropriate only for a genuinely single-operator
process (a CLI script) with no real caller to distinguish, not a
multi-user web app.

### Coverage note — the opposite of the MAF skill's warning

Once the Runner (or the `extra_plugins` registration) is wired, **new
sub-agents added later under the same root/Runner are automatically
governed** — no re-instrumentation needed, unlike a MAF project (see the
parapet-maf skill's own "Coverage is per-construction-site" section for
the contrast). The only thing that needs re-checking later is whether a
*new, separate* `Runner`/`extra_plugins` registration gets added
somewhere else in the project.

- Before calling this step done, actually construct the Runner/plugin (or
  at minimum import the wired module) rather than stopping at static file
  edits — a wrong kwarg name won't show up from editing alone.

## Base-URL interception (gateway) — the framework-agnostic alternative

For any OpenAI-SDK-shaped client, regardless of framework: set
`OPENAI_BASE_URL` to the gateway's URL and forward the agent's
`agent_id`/`agent_secret` however that project already manages secrets
(env file, secret manager — match its existing convention, don't invent a
new one). ADK's own model client (`google-genai`) does not speak this
shape (it's Gemini's own `:generateContent` wire format, not OpenAI's) —
this alternative only applies if the target project is *also* routing
through an OpenAI-compatible client for some other purpose, not as a
substitute for the in-process wiring above.

Either way:

- Look for an existing `.env`/`.env.local` first. **Ask before
  overwriting** any existing Parapet-related values in it — don't
  silently clobber a developer's prior setup.
- Use the exact env var names and install command from
  `parapet_get_quickstart`, not the ones written above (this doc can
  drift from a specific deployment; the tool call is the source of
  truth).

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
