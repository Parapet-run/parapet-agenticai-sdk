---
name: parapet-quickdemo
description: Use when the user wants to see Parapet work end to end without an existing project -- "build me a Parapet demo", "show me governance in action", "generate an example governed agent", or when parapet_getting_started's menu option 1 is picked. Generates a runnable, self-contained project demonstrating identity-based tool access (two people in different orgs, one tool each) for either Google ADK or Microsoft Agent Framework, with a mock model by default and a real Parapet control plane behind it. Requires the parapet MCP server (parapetai-mcp) to be connected. Distinct from parapet-adk/parapet-maf, which retrofit an EXISTING project -- this skill creates a new one from nothing.
---

# Parapet quickdemo: identity-based governance, from nothing

This skill generates a small, runnable project that demonstrates the thing
Parapet actually does: two people in different parts of the org (Tony in
Sales, Sally in HR) share one agent with two tools (`salesforce_lookup`,
`hr_lookup`). Without Parapet, either of them can call either tool. With
Parapet, a Cedar policy scoped to each person's org lets Tony reach
Salesforce and Sally reach HR — and denies each of them the other's tool.
The generated project runs this both ways and shows the difference on a
real, running control plane the user can click into.

Unlike **parapet-adk**/**parapet-maf** (which retrofit a project that
already exists), this skill creates a new project from nothing, in a
directory the user names.

## 0. Ask which framework, before generating anything

Ask the user directly: **Google ADK or Microsoft Agent Framework?**
(A third option, AWS Bedrock AgentCore, does not have a Parapet SDK
integration yet — if asked for it, say so plainly and stop; do not
generate an ADK or MAF project and call it AgentCore, and do not
improvise an integration that doesn't exist in `parapetai-agent`.)

Also ask where to generate the project (a directory name/path) if the
user hasn't already said.

## 1. Check prerequisites

Call `parapet_check_prerequisites`. If `all_ok` is `true`, say nothing
about it and move straight to step 2 — this is a silent gate, not
something to report on when nothing's wrong.

If `all_ok` is `false`, stop here and switch to the
**parapet-install-prereqs** skill's flow before doing anything else:
report each `"ok": false` entry's `detail` and `install_cmd`, and ask
before running any of them, one at a time (that skill — installed
alongside this one at `../parapet-install-prereqs/SKILL.md` — has the
exact wording and ordering rules, e.g. on macOS Homebrew has to succeed
before `pipx`/`uv`'s own commands will). Re-run
`parapet_check_prerequisites` after each approved install to confirm it
actually flipped to `"ok": true` before moving on. Only continue to step
2 of *this* skill once `all_ok` is `true`.

## 2. Check auth

Call `parapet_whoami`. All `parapet_*` tools default to the hosted control
plane at `https://app.parapet.run` unless the MCP server was registered
with a `PARAPETAI_CONTROL_PLANE_URL` override — don't pass a different
`control_plane_url` from memory or guesswork.

If `parapet_whoami` returns an `error` (not logged in):
1. Call `parapet_login`. It prints a URL and a short code.
2. Tell the user to open that URL and approve the login in their browser.
3. `parapet_login` polls internally and returns once approved.
4. Re-run `parapet_whoami` to confirm, and **keep its `account_id`** —
   every agent's console URL is scoped under `/a/{account_id}/...`, so
   this is needed later, not optional.

Never ask the user to paste a token, an agent secret, or any other
credential into the chat.

## 3. Provision one agent

Call `parapet_provision_agent` **once**, with
`display_name="quickdemo-<framework>-governed"`. This is the only agent
the demo needs — `example_no_governance.py` never calls the control
plane at all (that's the point of the contrast), so a second, provisioned
agent for it would have nothing to show on its page. Don't provision one.

The call returns `{agent_id, secret}` — **the secret is shown exactly
once.** Write it straight into the generated `.env` in step 6; never just
print it and move on.

## 4. Push the org policy to the governed agent

Read `templates/<framework>/policy/40-org.cedar` (next to this SKILL.md,
in this skill's own installed directory — do not write this content from
memory, read the actual file) and call:

```
parapet_push_policy_file(agent_id=<governed agent_id>, filename="40-org.cedar", content=<file content>)
```

This is what makes the governed example's denials real: the starter
bundle a fresh agent gets on provisioning permits everything, and Cedar
is default-deny past that — the org-scoped forbid rules in this file are
what makes Tony denied on `hr_lookup` and Sally denied on
`salesforce_lookup`.

## 5. Get the deployment's own config (optional, for a real model later)

Call `parapet_get_quickstart` if the user wants to know this deployment's
default model / install string for the framework they picked. Not needed
to run the demo itself — the demo mocks the model by default (step 7).

## 6. Generate the project

Read every file under `templates/<framework>/` (next to this SKILL.md)
and write it into the target directory, unchanged, **except** `.env`:
create it from `.env.example` with these substitutions filled in (the
generated project has no other way to learn them):

| Placeholder in `.env.example` | Value |
|---|---|
| `PARAPETAI_AGENT_ID` | the agent's `agent_id` (step 3) |
| `PARAPETAI_AGENT_SECRET` | the agent's `secret` (step 3) |
| `PARAPETAI_ACCOUNT_ID` | `account_id` from `parapet_whoami` (step 2) |
| `PARAPETAI_CONTROL_PLANE_URL` | the control plane URL used in step 2 |

Leave `OPENAI_API_KEY` (MAF) / `GOOGLE_API_KEY` (ADK) unset — the mock
model is the default and needs no key. Never write a real key into `.env`
on the user's behalf; if they want a real model, tell them which line to
edit and let them paste their own key in.

Do not modify any other file's content — these were built and verified
end to end (real framework, real mock model, real Cedar decisions against
a live control plane) as part of this skill; the value they demonstrate
depends on running unmodified.

## 7. Run it

From the generated project directory:
```
uv sync   # or: python3 -m venv .venv && source .venv/bin/activate && pip install -e .
uv run python driver.py
```

Report the control-plane link `driver.py` prints at the end — tell the
user to click it to see the org policy, the allow/deny decisions, and
the traces for themselves. This is the payoff; don't just say "it
worked," point at where they can verify it.

## Non-negotiables

- Never print the agent secret, the cli token, or the contents of
  `~/.parapet/credentials.json` into chat once written to disk.
- Never suggest disabling fail-closed defaults, weakening the org policy,
  or switching credential mode to make something "just work" — if a call
  is denied, that IS the demo working, not a bug to route around.
- If `parapet_provision_agent` returns a permission error, tell the user
  their account role doesn't allow provisioning (viewer role) — don't
  retry, don't try another endpoint.
- If `parapet_push_policy_file` returns a permission error, same thing —
  bundle editing needs owner/admin, not viewer.
