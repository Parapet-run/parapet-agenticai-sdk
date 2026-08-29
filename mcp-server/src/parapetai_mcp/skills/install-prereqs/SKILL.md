---
name: parapet-install-prereqs
description: Use when the user asks to set up prerequisites for Parapet -- "install pipx", "set up uv", "what do I need before parapetai-mcp", "/parapet-install-prereqs" -- or when another parapet-* skill (quickdemo, adk, maf) is about to fail because Python 3.12+, pipx, or uv isn't on PATH. Detects the real OS and package manager via the parapet_check_prerequisites tool and asks before running each install command -- never installs anything without a per-step yes.
---

# Parapet: install prerequisites

Every `parapet-*` skill assumes Python 3.12+, `pipx`, and `uv` are already
on the machine. None of them check for this themselves, and none of them
install anything — that gap is what this skill closes, with one rule
above everything else:

**Never run an install command without asking first, one command at a
time.** Homebrew's install script, `uv`'s install script, and `winget`/`apt`
all modify the machine (new binaries, PATH changes, in Homebrew's case a
whole package manager) — report what's missing, propose the exact
command, wait for a yes, then run it. Detect-and-report is always safe to
do unprompted; running an installer never is.

## 1. Detect

Call `parapet_check_prerequisites`. It runs entirely on this machine (no
control-plane call, nothing sent anywhere) and returns real detection —
not a guess: on Linux it checks which of `apt`/`dnf` is actually present
before suggesting a command, so it never hands back an `apt` command on a
Fedora box. Read its `os`, `checks`, and `all_ok` fields; don't re-derive
any of this from `uname`/environment variables yourself, this tool
already did it correctly.

If `all_ok` is `true`, say so and stop — there's nothing to install.

## 2. Report, then confirm each missing piece

For each entry in `checks` where `"ok": false`, tell the user:
- what's missing (from `detail`)
- the exact command that would fix it (from `install_cmd`)

Then ask: **run this now?** Wait for an explicit yes before running
anything. If several things are missing, go one at a time — a `brew`-
dependent command (macOS's `pipx`/`uv` commands both are) needs Homebrew
installed first, so if `homebrew.ok` is `false`, that one has to run (and
succeed) before `pipx`/`uv`'s own commands will actually work; say this
explicitly rather than proposing three commands at once that fail after
the first.

Run an approved command with your own Bash tool, in the user's actual
shell environment — not by inventing a wrapper script. After running one,
don't assume it worked: re-call `parapet_check_prerequisites` and confirm
that specific check flipped to `"ok": true` before moving to the next
one. A `pipx`/`uv` install typically requires a fresh shell for `PATH` to
pick it up — if a just-installed tool still doesn't resolve, say so
plainly ("installed, but you'll need to restart your terminal — or I can
re-check after you do") rather than treating it as a failure.

## 3. When everything's ok

Once every check passes, say so and point at what's next — most likely
`pipx install parapetai-mcp` (if not already installed) or running the
`parapet-quickdemo`/`parapet-adk`/`parapet-maf` skill they actually
wanted, whichever brought them here.

## Non-negotiables

- Never run an install command the user hasn't explicitly approved, even
  if `parapet_check_prerequisites` shows it's clearly needed.
- Never chain multiple install commands into one approved action ("run
  all three?") — approval is per command, since each one is a real
  modification to the user's machine.
- Never fabricate an install command yourself if `parapet_check_prerequisites`
  didn't provide one (e.g. `package_manager.ok: false` on Linux, meaning
  neither `apt` nor `dnf` was found) — say the tool couldn't determine an
  exact command and ask the user how their system installs packages,
  don't guess a distro.
