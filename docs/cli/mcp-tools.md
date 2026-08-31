# MCP tools

`parapetai-mcp serve` exposes 8 tools and 1 prompt over MCP. These are
what the [skills](skills.md) call on your behalf — you can also invoke
them directly from any MCP client. Every tool that hits the control plane
takes an optional `control_plane_url` argument, defaulting to
`PARAPETAI_CONTROL_PLANE_URL` (itself defaulting to
`https://app.parapet.run`) — don't pass a different one from memory or
guesswork; the registered default is authoritative.

Every tool that can fail returns `{"error": ...}` rather than raising —
consistent across all of them, so a caller only needs one error-handling
shape.

## `parapet_login`

```python
parapet_login(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> str
```

Authenticate as yourself against a Parapet control plane. Opens the
approval page in your default browser (falls back to printing the URL if
that fails — e.g. no GUI available); sign in if needed, and approve. This
tool polls until you do, and stores the resulting credential locally — **it
never returns the credential itself.**

Returns a human-readable status string, e.g.
`"Logged in to https://app.parapet.run (account acct_...)."`.

## `parapet_whoami`

```python
parapet_whoami(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> dict
```

Who you're authenticated as, and which agents already exist in your
account. Run `parapet_login` first if this reports not logged in. The
returned `account_id` is needed for every agent console link
(`/a/{account_id}/agents/{agent_id}`), so treat it as required output, not
optional.

## `parapet_get_quickstart`

```python
parapet_get_quickstart(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> dict[str, str]
```

The exact SDK install command, minimum Python version, default model, and
env var names *this specific control-plane deployment* expects — always
read from the deployment's own config, never hardcoded, so it can't drift
from what `pip install` actually resolves to there.

## `parapet_provision_agent`

```python
parapet_provision_agent(
    display_name: str | None = None,
    tenant_id: str | None = None,
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL,
) -> dict
```

Provision a new governed agent in your account. Returns `{agent_id,
secret}` — **the secret is shown exactly once here**, the same as
everywhere else in this system. Write it straight into the target
project's env config; don't just print it and move on.

## `parapet_list_agents`

```python
parapet_list_agents(control_plane_url: str = DEFAULT_CONTROL_PLANE_URL) -> dict
```

Read-only: the agents already provisioned in your account.

## `parapet_push_policy_file`

```python
parapet_push_policy_file(
    agent_id: str,
    filename: str,
    content: str,
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL,
) -> dict
```

Write one Cedar policy file into an already-provisioned agent's bundle
(creates it if the filename is new, overwrites if it already exists).
Requires an owner/admin role — a viewer's token gets a `PermissionError`
surfaced as `{"error": ...}`, same shape as every other tool here. Use
this instead of asking a human to paste policy into the console by hand
when the content was already generated (e.g. by `parapet-quickdemo`).

## `parapet_check_prerequisites`

```python
parapet_check_prerequisites() -> dict
```

Local-machine check — **no control plane call, nothing sent anywhere** —
for what every `parapet-*` skill assumes is already installed: Python
3.12+, `pipx`, and `uv`. Detects the real OS and (on Linux) which package
manager is actually present, so the returned `install_cmd` is never a
guess — e.g. `apt` on a Fedora box would be wrong. **Does not install
anything itself**; a caller reports the results and asks before running
any install command (see the `parapet-install-prereqs` skill).

Return shape:

```json
{
  "os": "...",
  "python_executable": "...",
  "checks": {
    "python": {"ok": true, "detail": "..."},
    "pipx": {"ok": false, "detail": "...", "install_cmd": "..."},
    "uv": {"ok": true, "detail": "..."}
  },
  "all_ok": false
}
```

`checks` always includes `python`, `pipx`, `uv`, plus `homebrew` (macOS),
`package_manager` (Linux), or `winget` (Windows).

## `parapet_audit_codebase`

```python
parapet_audit_codebase(path: str, output_dir: str | None = None) -> dict
```

Local, static AST scan — **no control plane call, nothing sent anywhere**,
same locality guarantee as `parapet_check_prerequisites` — of an existing
Python codebase for ungoverned model/tool-call sites: a raw
`agent_framework.Agent` or `google.adk.runners.Runner`/`InMemoryRunner`
construction (especially one with `tools=`), a `build_middleware()`/
`build_plugin()` result never registered into `middleware=`/`plugins=`, a
raw `openai`/`anthropic`/`google.genai` client with no governance visible
in the same file, or a `GovernedAgent`/`GovernedRunner` relying only on
the SDK's generic bundled default policy.

Deliberately favors **precision over recall** — a narrow, hand-verified
set of known import/call shapes rather than a fuzzy heuristic that would
flag arbitrary `.create(`-shaped calls across the whole codebase. It can
miss a real ungoverned call site it has no pattern for, but shouldn't
report a false high. **A clean result is not a certification** — see
`files_skipped` in the return value and the report's own header before
treating an empty findings list as proof of governance.

Each finding is scored `high`/`medium`/`low` and carries a `framework`
field (`"maf"`, `"adk"`, or `null`) so a follow-up fix pass knows which of
`GovernedAgent`/`GovernedRunner` applies, if either does. Written to a
Markdown report at `{output_dir or path/.parapet/audit}/report.md` and
also returned inline as `findings`. See [Skills](skills.md#parapet-audit)
for the read-only audit workflow and
[parapet-audit-fix](skills.md#parapet-audit-fix) for the follow-up pass
that acts on it.

## `parapet_getting_started` (prompt, not a tool)

```python
parapet_getting_started() -> str
```

First-run menu for a newly connected Parapet MCP server — surfaced as a
pickable starter prompt by clients that list MCP prompts (e.g. Claude
Code's `/mcp` menu). No MCP mechanism fires a prompt automatically on
connect, so a client/skill that wants "ask before doing anything"
behavior needs to invoke this explicitly as its first move, rather than
assuming it already ran. Returns a static 4-option menu: build a demo via
`parapet-quickdemo`, set up prerequisites via `parapet-install-prereqs`,
audit an existing codebase via `parapet-audit`/`parapet-audit-fix`, or
list the raw tools.
