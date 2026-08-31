---
name: parapet-audit-fix
description: Use after parapet-audit has produced a report (or when the user asks to "fix the audit findings", "wrap the flagged calls in GovernedAgent/GovernedRunner", "apply the governance fixes") to act on that report -- provisions agents and instruments the flagged construction sites. Requires the parapet MCP server (parapetai-mcp) to be connected, and a report already saved by parapet-audit (run that skill first if none exists). Modifies the target codebase's files -- unlike parapet-audit, which is read-only.
---

# Parapet audit-fix: act on a saved audit report

This skill reads the Markdown report `parapet-audit` produces and applies
fixes to the flagged construction sites — **it edits real files**, unlike
`parapet-audit`'s read-only scan. Confirm the user actually wants that
before starting, if it isn't already obvious from how they asked.

## 1. Locate the report

Default location: `<audited path>/.parapet/audit/report.md`. If you don't
already know the path from a prior `parapet-audit` run in this
conversation, ask the user, or run `parapet_audit_codebase(path=...)`
yourself first — don't assume a report exists or guess its content from
memory. A fix pass without a fresh scan risks acting on stale findings if
the code has changed since.

## 2. Triage the findings — different categories need different fixes

Read every row in the report's Findings table. **Not every finding gets
the same treatment** — match the fix to the category, don't force
`GovernedAgent`/`GovernedRunner` onto something it doesn't apply to:

| Category | What it means | Fix |
|---|---|---|
| `ungoverned-tool-call` / `ungoverned-model-call` | A raw `agent_framework.Agent` or `google.adk.runners.Runner`/`InMemoryRunner` construction | Follow **step 3** below — this is the actual "wrap in GovernedAgent/GovernedRunner" fix. |
| `ungoverned-registration` | `build_middleware()`/`build_plugin()` called but the result never reached `middleware=[...]`/`plugins=[...]` | A smaller, mechanical fix — wire the already-built middleware/plugin into the construction site's kwarg. No new agent needs provisioning; the governance objects already exist, they're just not attached. |
| `generic-policy-only` | Already `GovernedAgent`/`GovernedRunner`, but with no `policy_dir`/`control_plane_url` — enforcing only the SDK's generic bundled default | Not a wrap — add real policy. Ask whether the user wants a local `policy_dir` (fast, no control plane) or a control-plane-managed agent (`parapet_provision_agent` + `parapet_get_quickstart`, same as steps 2-3 of parapet-maf/parapet-adk). |
| `raw-model-client` | A raw `openai`/`anthropic`/`google.genai` client with no framework in use | **Not** a GovernedAgent/GovernedRunner case — neither applies without MAF or ADK underneath. Recommend `Governor.check_input()`/`.authorize_tool()`/`.check_output()` wrapped around the call sites by hand; don't force-fit a framework adapter that isn't there. |
| `framework-present-no-governance` | The dependency file lists `agent-framework`/`google-adk` but `parapetai-agent` isn't installed anywhere | Install the extra first (`pip install "parapetai-agent[maf]"` or `[adk]`) before any per-site fix makes sense. |
| `governed` | Already fine | Nothing to do. |

## 3. Fixing `ungoverned-tool-call`/`ungoverned-model-call` findings

Each finding's `framework` field (`maf` or `adk`) tells you which sibling
skill's instrumentation procedure applies — **use that skill's actual
steps, don't improvise a shorter version**:

- `framework: "maf"` → follow **parapet-maf**'s "1. Check auth" through
  "4. Wire the target project" (including its "Coverage is
  per-construction-site" section — a MAF fix only covers the exact
  `Agent(...)` you swap, not every construction site in the codebase, so
  re-grep after fixing the ones the report flagged).
- `framework: "adk"` → follow **parapet-adk**'s "1. Check auth" through
  "4. Wire the target project" (its Shape A / Shape B branch, and its
  "Coverage note" — one `Runner`/`extra_plugins` fix covers everything
  downstream of it, genuinely different from MAF's per-site model).

**Provision one agent, not one per finding.** If the report has multiple
MAF findings, they likely belong to the same project and can share one
`parapet_provision_agent` call — don't create a new agent per file unless
the user actually wants per-service identities. Ask if it's ambiguous
(e.g. findings span what look like genuinely separate services).

Group the findings by `framework` first, handle all `maf` findings
together (one provisioning pass, then every MAF construction site), then
all `adk` findings together, rather than interleaving.

## 4. Fixing `ungoverned-registration` findings

No new agent needed — the `build_middleware()`/`build_plugin()` call
already exists and already has real `agent_id`/`control_plane_url`
wherever it's defined. Just wire its result into the right kwarg at the
Agent/Runner construction site the report points at (or nearby, if the
construction site is a different line than the report's flagged line —
the AST scanner flags the `build_middleware()`/`build_plugin()` call
itself, not necessarily the Agent/Runner it should feed).

## 5. Show your work, then re-run the audit

After applying fixes for a batch (don't wait until every single finding
across the whole report is done if there are many — a natural checkpoint,
e.g. "all MAF findings," is fine), summarize exactly what changed
(file:line, before → after) rather than just saying "fixed." Then re-run:

```
parapet_audit_codebase(path=<the same audited path>)
```

and report the new high/medium/low counts against the original — this is
the actual proof the fix worked, not just that files were edited. If a
finding is still present after a fix that should have addressed it,
investigate why before moving on (a typo'd import, a construction site
the fix missed) rather than reporting success prematurely.

## Non-negotiables

- Never print the agent secret, the cli token, or the contents of
  `~/.parapet/credentials.json` into chat once written to disk (same
  rule as parapet-maf/parapet-adk).
- Never wrap a `raw-model-client` or `framework-present-no-governance`
  finding in `GovernedAgent`/`GovernedRunner` directly — neither
  framework is actually present for those; recommend `Governor` or
  installing the extra instead, per the triage table above.
- Never weaken or remove an existing policy/kwarg to make a construction
  "compile" faster — if a real fix needs a `policy_dir` or control-plane
  binding the user hasn't provided yet, say so and ask, don't invent a
  permit-everything local policy to paper over it.
- If `parapet_provision_agent` returns a permission error, tell the user
  their account role doesn't allow provisioning (viewer role) — don't
  retry, don't try another endpoint.
- Confirm before editing more than a handful of files in one pass on a
  codebase you haven't touched before this session — a large mechanical
  edit is worth a quick "here's what I'm about to change" first.
