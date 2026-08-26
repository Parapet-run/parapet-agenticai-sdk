---
name: parapet-maf
description: Use when the user asks to govern a Microsoft Agent Framework (agent_framework) agent with Parapet, add Parapet to a project that already uses agent_framework, provision a Parapet agent for a MAF project, or wire up Parapet/parapetai-agent[maf] env vars. Requires the parapet MCP server (parapetai-mcp) to be connected. For a Google ADK (google-adk) project, use the parapet-adk skill instead — the instrumentation procedure is genuinely different, not just a naming difference.
---

# Parapet + Microsoft Agent Framework: provision + instrument

This skill drives the `parapet_*` MCP tools to authenticate, provision a
governed agent, and instrument a Microsoft Agent Framework (`agent_framework`)
codebase to route through it. The tools only talk to the control plane —
**you** make the actual file edits, with your normal file tools, following
the steps below.

If the target project uses Google ADK (`google-adk`) instead — check for
`from google.adk...`/`import google.adk` before assuming this is the right
skill — use the **parapet-adk** skill instead. The two frameworks put
governance in genuinely different places (see step 4 below), so applying
this skill's instructions to an ADK project, or vice versa, will not work.

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
MAF-named fields specifically: `sdk_install` (not `sdk_install_adk`, that's
the Google ADK one) and `default_model` (not `default_model_adk`). `python_min`
is shared (one Python floor for the whole `parapetai-agent` package,
regardless of extra). Always use these values, never hardcode a
`pip install` string or env var name from memory, since a different
deployment can configure all of them differently.

## 4. Wire the target project

One import line, per `parapetai_agent`'s own public API:

```python
from parapetai_agent import GovernedAgent as Agent, identity_from_claims
```

Alias it exactly as shown — **do not rename to match whatever class the
target project already imports** (e.g. don't alias it as `ChatAgent` just
because the project's existing code imports `ChatAgent`). `GovernedAgent`
subclasses `agent_framework.Agent` specifically (verify with
`GovernedAgent.__mro__`, not memory). Class/kwarg/env-var names in
`agent_framework`/MAF are not stable across versions and `parapetai-agent[maf]`
pulls a floating `agent-framework<2.0,>=1.13` range, so the version actually
installed can be newer than whatever the target project's existing code was
written against — e.g. `ChatAgent` doesn't exist in a current install
(`hasattr(agent_framework, "ChatAgent")` is `False`; only `Agent`, kwarg
`client=`, does) even though the SAME installed package's own docstrings
still show stale `ChatAgent(...)` example code in several places. Don't
extrapolate a specific old-name → new-name mapping from memory or from an
error message alone, either — verify the ACTUAL current name by inspecting
what's installed: `GovernedAgent.__init__` itself forwards everything via
`**kwargs` (its own signature won't show real kwarg names), so check the
base class from `GovernedAgent.__mro__`, or the relevant provider client
class directly (e.g. `agent_framework.openai.OpenAIChatCompletionClient`),
with `inspect.signature(...)`.

Construct it with `agent_id=`, `agent_secret=`, `control_plane_url=` from
steps 2–3.

### Coverage is per-construction-site, not automatic — read this before declaring done

MAF has no centralized place governance gets registered once and covers
everything after that. There is no MAF equivalent of "wrap the Runner,
every agent underneath it is governed" (that's Google ADK's model, not
this one — see the parapet-adk skill). Every single `agent_framework.Agent(...)`
construction in the target codebase governs only ITSELF, independently. A
project with a multi-agent workflow (`agent_framework.AgentExecutor`,
`FunctionalWorkflowAgent`, or any hand-rolled orchestration passing one
`Agent` to another) needs **every** `Agent` node individually constructed
as `GovernedAgent` — wrapping the top-level/orchestrator agent does
**not** govern the agents it delegates to.

Concretely:

1. **Find every construction site, not just one.** Grep the whole target
   codebase for `agent_framework.Agent(`, `from agent_framework import`
   (checking every match for `Agent` in the imported names, including
   `Agent as X` aliases already in use), and any factory function that
   internally constructs one. A single file swapping its own import to
   `from parapetai_agent import GovernedAgent as Agent` governs every
   `Agent(...)` call *within that same file* (since it's now the same
   imported name pointing at a different class) — it does **not** govern
   `Agent(...)` calls in any *other* file that imports `Agent` from
   `agent_framework` independently. Update every one you find, using the
   identical `from parapetai_agent import GovernedAgent as Agent` line
   (or `from parapetai_agent import GovernedAgent` plus
   `agent_framework.Agent(...)` call sites → `GovernedAgent(...)`,
   matching however that file already imports agent_framework).
2. **Verify, don't just edit.** After wiring, grep again for
   `agent_framework.Agent(` and `from agent_framework import Agent`
   (without an `as GovernedAgent`/aliasing edit) across the whole
   codebase. Anything still matching is an ungoverned construction site —
   go back and fix it before declaring this step done, the same "verify
   at construction/runtime, not just from editing" discipline the note
   below already asks for.
3. **This is a point-in-time transformation, not an ongoing guarantee.**
   If the developer adds a *new* `Agent(...)` construction later (a new
   file, a new module, a new workflow node) without following the same
   convention, it is silently ungoverned — MAF gives no runtime signal
   that an unwrapped `Agent` exists alongside governed ones. There are two
   ways to keep coverage complete going forward, and the user should know
   both:
   - Re-run this skill (ask the agent to re-check the project) whenever
     new `Agent(...)` construction is added, so a fresh grep-and-fix pass
     catches anything new.
   - Establish the convention explicitly in the project (e.g. a
     `CONTRIBUTING.md`/lint rule banning bare `from agent_framework import
     Agent`) so new code follows it without needing a re-run at all.
   Tell the user this explicitly once instrumentation is done — don't let
   them assume "wire it once" means "covered forever" the way it would
   for an ADK project.

- Before calling this step done, actually construct the agent object (or
  at minimum import the wired module and run `inspect.signature(...)` on
  the relevant class — see the base-class/provider-client note above,
  not `GovernedAgent.__init__` itself) rather than stopping at static
  file edits. A wrong kwarg name or renamed base class won't show up from
  editing alone — it only surfaces at construction/runtime, and catching
  it here means one pass instead of the user pasting back a traceback for
  you to debug separately.

## Base-URL interception (gateway) — the framework-agnostic alternative

For any OpenAI-SDK-shaped client, regardless of framework: set
`OPENAI_BASE_URL` to the gateway's URL and forward the agent's
`agent_id`/`agent_secret` however that project already manages secrets
(env file, secret manager — match its existing convention, don't invent a
new one). This is a real alternative to the in-process `GovernedAgent`
wiring above, not a fallback for when it doesn't apply — ask which fits
the project if it isn't obvious.

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
