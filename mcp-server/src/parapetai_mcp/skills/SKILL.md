---
name: parapet
description: Use when the user asks to govern an agent with Parapet, add Parapet to this project, provision a Parapet agent, or wire up Parapet/parapetai-agent env vars. Requires the parapet MCP server (parapetai-mcp) to be connected.
---

# Parapet: provision + instrument an agent

This skill drives the `parapet_*` MCP tools to authenticate, provision a
governed agent, and configure a codebase to route through it. The tools
only talk to the control plane — **you** make the actual file edits, with
your normal file tools, following the steps below.

## 1. Check auth

Call `parapet_whoami`. If it returns an `error` (not logged in):

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
actual `sdk_install` command, `python_min` version, and `default_model` —
always use these values, never hardcode a `pip install` string or env var
name from memory, since a different deployment can configure all of them
differently.

## 4. Wire the target project

Ask which integration style fits before choosing for the user if it isn't
obvious from the project (look for an existing `agent_framework`/MAF usage
vs. a direct OpenAI/Anthropic client):

**In-process (parapetai-agent SDK)** — a project already using
`agent_framework`/MAF. One import line, per `parapetai_agent`'s own public
API:

```python
from parapetai_agent import GovernedAgent as Agent, identity_from_claims
```

Construct it with `agent_id=`, `agent_secret=`, `control_plane_url=` from
steps 2–3.

**Base-URL interception (gateway)** — any OpenAI-SDK-shaped client. Set
`OPENAI_BASE_URL` to the gateway's URL and forward the agent's
`agent_id`/`agent_secret` however that project already manages secrets
(env file, secret manager — match its existing convention, don't invent a
new one).

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
