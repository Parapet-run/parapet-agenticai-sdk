---
name: parapet-audit
description: Use when the user asks to audit, scan, or check an existing codebase for AI-agent governance risks -- "find ungoverned model/tool calls", "scan this repo for agent security risks", "how exposed are we without Parapet", or when parapet_getting_started's menu option 3 is picked. Runs a local, static AST scan (no control plane call, nothing sent anywhere) and saves a scored Markdown report. Does NOT modify any code -- that's the separate parapet-audit-fix skill, run after this one.
---

# Parapet audit: find ungoverned model/tool calls

This skill runs a **read-only** static scan of an existing Python codebase
and produces a scored report of where model/tool calls are ungoverned —
it never edits code. Use **parapet-audit-fix** afterward to act on the
findings.

## 1. Confirm the target directory

Ask which directory to audit if the user hasn't already said (default to
the current project root if that's clearly what they mean — don't assume
silently for an ambiguous request spanning multiple projects).

## 2. Run the scan

```
parapet_audit_codebase(path=<target directory>)
```

No login, no control plane call, nothing sent anywhere — this is a local
filesystem + AST operation only, the same locality guarantee as
`parapet_check_prerequisites`. Don't ask the user to authenticate for
this step.

Optionally pass `output_dir` if the user wants the report saved somewhere
other than the default `<path>/.parapet/audit/report.md`.

## 3. Report the results — don't just say "done"

Summarize the high/medium/low counts, then walk through the **HIGH**
findings by name (file:line + what's wrong) — these are the ones with a
real, unauthorized tool-execution or governance-registration bug. Point
at the saved report path so the user can open the full table themselves,
and mention the `files_skipped` list if it's non-empty (files the scanner
couldn't parse — real blind spots, not silently-clean areas).

**Be honest about what this scanner is and isn't**, using its own report
header as the basis, not your own paraphrase that might drop the caveat:
it's a precision-favoring, best-effort static scan against a curated set
of known import/call shapes (Microsoft Agent Framework, Google ADK, and a
handful of raw model clients) — it can miss a real ungoverned call site it
has no pattern for, but shouldn't produce a false HIGH. **A clean result
is not a certification.** Don't let the user walk away thinking zero
findings means zero risk if the report itself says files were skipped or
the target doesn't use a framework this scanner recognizes.

## 4. Offer the fix

If there are any HIGH or MEDIUM findings, tell the user they can run the
**parapet-audit-fix** skill next to wrap the flagged construction sites in
`GovernedAgent`/`GovernedRunner` and re-confirm the count drops — don't
apply fixes yourself from this skill; that's a separate, explicit step so
a read-only audit never turns into an unplanned code-editing session.

## Non-negotiables

- This skill **never edits, deletes, or moves any file** in the audited
  codebase — only `.parapet/audit/report.md` (or the user's chosen
  `output_dir`) gets written, and that's the tool's own job, not yours.
- Don't characterize a clean scan as "your codebase is secure" or
  "fully governed" — say what the scanner actually found (or didn't),
  with its own precision-over-recall caveat attached.
- If the target path doesn't exist or isn't a directory, say so plainly —
  don't guess at a nearby path.
