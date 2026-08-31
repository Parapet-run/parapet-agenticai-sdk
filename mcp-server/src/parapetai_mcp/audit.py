"""Static, AST-based scan for ungoverned model/tool-call sites in a Python
codebase -- no control plane call, nothing sent anywhere, same locality
guarantee as parapet_check_prerequisites.

Deliberately favors PRECISION over RECALL: a narrow, hand-verified set of
canonical import/call shapes (agent_framework.Agent, google.adk.runners.
Runner/InMemoryRunner, parapetai_agent's Governed* classes and
build_middleware/build_plugin, plus a short list of raw model-client
constructors) rather than a fuzzy heuristic that would flag arbitrary
`.create(`-shaped calls across the whole codebase. Every finding this
module reports should be one a human can immediately verify by looking at
the cited file:line -- a scanner that cries wolf trains people to ignore
it. What this means concretely: this scanner can under-report (miss a
real ungoverned call site it has no pattern for) but should essentially
never over-report a false HIGH. Report `files_skipped` honestly so a
reader knows where its blind spots are, rather than presenting a clean
report as if it were exhaustive.

Two-pass per file: pass 1 (visit Import/ImportFrom) builds an alias map
resolving whatever local name a construct is imported/aliased as back to
its canonical dotted name -- this is what lets
`from parapetai_agent import GovernedAgent as Agent` correctly resolve
`Agent(...)` as GOVERNED even though the local name collides with the raw
class's own conventional name (exactly the pattern the parapet-maf skill
teaches). Pass 2 (ast.walk) finds every Call/Assign of interest and
resolves each callee through that alias map.
"""

from __future__ import annotations

import ast
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Severity = Literal["high", "medium", "low"]

# Directories never worth descending into -- generated, vendored, or
# dependency code, not the target project's own source.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".parapet",
    ".idea",
    ".vscode",
}
_MAX_FILE_BYTES = 2 * 1024 * 1024  # skip anything not plausibly hand-written source
_MAX_FILES_SCANNED = 5000  # a runaway target (e.g. pointed at $HOME) stops here, not OOM/hang

# Canonical dotted name -> which framework it belongs to.
_RAW_AGENT_CTORS = {"agent_framework.Agent": "maf"}
_GOVERNED_AGENT_CTORS = {
    "parapetai_agent.GovernedAgent": "maf",
    "parapetai_agent.maf.GovernedAgent": "maf",
}
_RAW_RUNNER_CTORS = {
    "google.adk.runners.Runner": "adk",
    "google.adk.runners.InMemoryRunner": "adk",
}
_GOVERNED_RUNNER_CTORS = {
    "parapetai_agent.adk.GovernedRunner": "adk",
    "parapetai_agent.adk.InMemoryGovernedRunner": "adk",
}
_BUILD_MIDDLEWARE_FUNCS = {
    "parapetai_agent.maf.build_middleware",
    "parapetai_agent.build_middleware",
}
_BUILD_PLUGIN_FUNCS = {
    "parapetai_agent.adk.build_plugin",
    "parapetai_agent.build_plugin",
}
# Raw model-CLIENT constructors only (not arbitrary `.create(`/`.generate_
# content(` method calls -- see module docstring on precision-over-recall).
# A raw client existing doesn't by itself prove an ungoverned call, but a
# raw client with NO Governed*/Governor usage anywhere else in the same
# file is a reasonable, low-noise MEDIUM signal.
_RAW_MODEL_CLIENTS = {
    "openai.OpenAI",
    "openai.AsyncOpenAI",
    "anthropic.Anthropic",
    "anthropic.AsyncAnthropic",
    "google.genai.Client",
}
# Substring match, case-insensitive, against a tool function's own name --
# curated for genuinely destructive/high-blast-radius verbs, not every verb
# that could plausibly write something (avoids flooding findings with e.g.
# "update_profile"). Extend this list rather than widen the matching logic.
_DESTRUCTIVE_TOOL_HINTS = (
    "delete",
    "drop",
    "remove",
    "truncate",
    "destroy",
    "purge",
    "wipe",
    "shell",
    "exec",
    "eval",
    "wire_transfer",
    "transfer_funds",
    "payment",
    "refund",
    "grant_access",
    "revoke_access",
    "admin",
)


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    category: str
    file: str  # POSIX-style, relative to the audited root
    line: int
    message: str
    recommendation: str
    # "maf" | "adk" | None -- explicit so a fix pass (parapet-audit-fix)
    # doesn't have to parse framework out of `message` prose to know which
    # of GovernedAgent/GovernedRunner applies.
    framework: str | None = None


@dataclass(slots=True)
class _FileFacts:
    """What one file's AST walk collected, before cross-checks that need
    the whole file's picture (e.g. was a build_middleware() result ever
    registered into middleware=[...])."""

    findings: list[Finding] = field(default_factory=list)
    has_governed_usage: bool = False
    has_raw_client_ctor: list[int] = field(default_factory=list)  # line numbers
    # (target names, line) for a build_middleware()/build_plugin() call
    # assigned in this file -- framework is implicitly "maf"/"adk" by
    # which list it's in, so no separate tag field is needed.
    middleware_bindings: list[tuple[tuple[str, ...], int]] = field(default_factory=list)
    plugin_bindings: list[tuple[tuple[str, ...], int]] = field(default_factory=list)
    registered_names: set[str] = field(default_factory=set)
    uses_parapetai_agent: bool = False  # real import parse, not a substring search


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class _ImportVisitor(ast.NodeVisitor):
    """Pass 1: local name -> canonical dotted name, for both plain names
    (`from X import Y as Z`) and module-shaped bindings (`import X.Y as Z`,
    `from X import Y` where Y is itself a submodule -- ast can't tell the
    two apart at parse time, but the resulting dotted-name arithmetic is
    identical either way, so one map serves both)."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}
        self.saw_parapetai_agent = False

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            canonical = alias.asname and alias.name or alias.name.split(".")[0]
            self.aliases[bound] = canonical
            self._note_framework(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:  # relative `from . import x` -- nothing to resolve globally
            return
        for alias in node.names:
            bound = alias.asname or alias.name
            self.aliases[bound] = f"{node.module}.{alias.name}"
            self._note_framework(node.module)

    def _note_framework(self, module: str) -> None:
        if module.startswith("parapetai_agent"):
            self.saw_parapetai_agent = True


def _resolve(func: ast.expr, aliases: dict[str, str]) -> str | None:
    dotted = _dotted_name(func)
    if dotted is None:
        return None
    if isinstance(func, ast.Name):
        return aliases.get(dotted, dotted)
    first, sep, rest = dotted.partition(".")
    if first in aliases:
        return f"{aliases[first]}.{rest}" if sep else aliases[first]
    return dotted


def _literal_tool_names(node: ast.expr) -> tuple[list[str] | None, bool]:
    """Returns (names, is_literal). names is only meaningful when
    is_literal -- a dynamic tools= value (a variable, a function call, a
    comprehension) can't be inspected statically, and that's reported
    honestly rather than silently treated as empty."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None, False
    names: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Name):
            names.append(elt.id)
        elif isinstance(elt, ast.Attribute):
            names.append(elt.attr)
        # else: not a simple reference (e.g. a lambda) -- skip that element,
        # still counts toward "has tools" via the surrounding list's length
    return names, True


def _annotate_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _scan_source(source: str, rel_path: str) -> _FileFacts:
    facts = _FileFacts()
    tree = ast.parse(source, filename=rel_path)
    _annotate_parents(tree)

    imports = _ImportVisitor()
    imports.visit(tree)
    facts.uses_parapetai_agent = imports.saw_parapetai_agent
    aliases = imports.aliases

    # Pass A: collect every name that appears inside ANY middleware=[...]/
    # plugins=[...] keyword list literal, anywhere a Call has one -- covers
    # `Agent(middleware=[chat_mw, func_mw])`, `Runner(plugins=[plugin])`,
    # and `async with Agent(middleware=[...]) as agent:` alike, since this
    # doesn't care what statement the Call is embedded in.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in ("middleware", "plugins") and isinstance(kw.value, (ast.List, ast.Tuple)):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Name):
                        facts.registered_names.add(elt.id)

    # Pass B: resolve every Call against the alias map and branch by shape.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _visit_call(node, aliases, rel_path, facts)

    return facts


def _visit_call(node: ast.Call, aliases: dict[str, str], rel_path: str, facts: _FileFacts) -> None:
    resolved = _resolve(node.func, aliases)
    if resolved is None:
        return

    tools_kw = next((kw for kw in node.keywords if kw.arg == "tools"), None)

    if resolved in _RAW_AGENT_CTORS or resolved in _RAW_RUNNER_CTORS:
        _emit_ungoverned_construction(node, resolved, tools_kw, rel_path, facts)
        return

    if resolved in _GOVERNED_AGENT_CTORS or resolved in _GOVERNED_RUNNER_CTORS:
        facts.has_governed_usage = True
        _emit_governed_construction(node, resolved, rel_path, facts)
        return

    if resolved in _BUILD_MIDDLEWARE_FUNCS or resolved in _BUILD_PLUGIN_FUNCS:
        facts.has_governed_usage = True
        parent = getattr(node, "parent", None)
        if isinstance(parent, ast.Assign):
            target = parent.targets[0]
            if isinstance(target, ast.Name):
                names: tuple[str, ...] = (target.id,)
            elif isinstance(target, ast.Tuple):
                names = tuple(elt.id for elt in target.elts if isinstance(elt, ast.Name))
            else:
                names = ()
            if names:
                bucket = (
                    facts.middleware_bindings
                    if resolved in _BUILD_MIDDLEWARE_FUNCS
                    else facts.plugin_bindings
                )
                bucket.append((names, node.lineno))
        elif isinstance(parent, ast.Expr):
            # A bare statement: `build_middleware(...)` on its own line,
            # return value discarded immediately -- unambiguously broken.
            fn_name = resolved.rsplit(".", 1)[-1]
            fw = "maf" if resolved in _BUILD_MIDDLEWARE_FUNCS else "adk"
            facts.findings.append(
                Finding(
                    severity="high",
                    category="ungoverned-registration",
                    file=rel_path,
                    line=node.lineno,
                    message=(
                        f"{fn_name}() is called but its return value is discarded -- the "
                        "governance middleware/plugin it built is never registered on an "
                        "Agent/Runner, so this line enforces nothing."
                    ),
                    recommendation=(
                        "Assign the result and pass it into middleware=[...] (MAF) or "
                        "plugins=[...]/app.plugins (ADK), or use GovernedAgent/GovernedRunner "
                        "directly instead."
                    ),
                    framework=fw,
                )
            )
        # Any other parent shape (inline in a list, passed straight as an
        # argument, etc.) is treated as "used somewhere" and not flagged --
        # precision over recall, per the module docstring.
        return

    if resolved in _RAW_MODEL_CLIENTS:
        facts.has_raw_client_ctor.append(node.lineno)


def _emit_ungoverned_construction(
    node: ast.Call,
    resolved: str,
    tools_kw: ast.keyword | None,
    rel_path: str,
    facts: _FileFacts,
) -> None:
    class_name = resolved.rsplit(".", 1)[-1]
    framework = _RAW_AGENT_CTORS.get(resolved) or _RAW_RUNNER_CTORS.get(resolved)
    governed_name = "GovernedAgent" if framework == "maf" else "GovernedRunner"

    if tools_kw is None:
        facts.findings.append(
            Finding(
                severity="medium",
                category="ungoverned-model-call",
                file=rel_path,
                line=node.lineno,
                message=(
                    f"Raw {class_name}(...) construction with no tools declared -- "
                    "prompts/responses through this agent bypass Cedar's model_call/"
                    "post-stage checks entirely (no PII/injection scanning, no "
                    "groundedness/judge eval)."
                ),
                recommendation=f"Swap the import for parapetai_agent's {governed_name}.",
                framework=framework,
            )
        )
        return

    names, is_literal = _literal_tool_names(tools_kw.value)
    destructive = [n for n in (names or []) if any(h in n.lower() for h in _DESTRUCTIVE_TOOL_HINTS)]

    if is_literal and destructive:
        message = (
            f"Raw {class_name}(...) construction with tools including "
            f"{', '.join(sorted(set(destructive)))} -- these run with NO authorization "
            "check at all: any caller, any argument, every time the model decides to "
            "call them."
        )
    elif is_literal:
        message = (
            f"Raw {class_name}(...) construction with tools={names!r} -- every one of "
            "these runs unauthorized; a denied-by-policy call today just... runs."
        )
    else:
        message = (
            f"Raw {class_name}(...) construction with a non-literal tools= value "
            "(can't enumerate statically) -- inspect manually. Whatever it resolves to "
            "runs unauthorized."
        )

    facts.findings.append(
        Finding(
            severity="high",
            category="ungoverned-tool-call",
            file=rel_path,
            line=node.lineno,
            message=message,
            recommendation=f"Swap the import for parapetai_agent's {governed_name}.",
            framework=framework,
        )
    )


def _emit_governed_construction(
    node: ast.Call, resolved: str, rel_path: str, facts: _FileFacts
) -> None:
    kwarg_names = {kw.arg for kw in node.keywords if kw.arg}
    has_real_policy = bool(
        kwarg_names & {"policy_dir", "control_plane_url", "agent_secret", "persist_policy_dir"}
    )
    class_name = resolved.rsplit(".", 1)[-1]
    framework = _GOVERNED_AGENT_CTORS.get(resolved) or _GOVERNED_RUNNER_CTORS.get(resolved)
    if has_real_policy:
        facts.findings.append(
            Finding(
                severity="low",
                category="governed",
                file=rel_path,
                line=node.lineno,
                message=f"{class_name}(...) construction with an explicit policy source.",
                recommendation="No action needed -- listed for audit-trail completeness.",
                framework=framework,
            )
        )
    else:
        facts.findings.append(
            Finding(
                severity="medium",
                category="generic-policy-only",
                file=rel_path,
                line=node.lineno,
                message=(
                    f"{class_name}(...) construction with no policy_dir/control_plane_url/"
                    "agent_secret -- this enforces only the SDK's bundled generic default "
                    "policy (permit model_call/tool_call broadly), not a real policy for "
                    "this project."
                ),
                framework=framework,
                recommendation=(
                    'Pass policy_dir="./policies" (local) or agent_id=/agent_secret=/'
                    "control_plane_url= (control-plane-managed) once real policy exists."
                ),
            )
        )


def _reconcile_registrations(facts: _FileFacts) -> None:
    for names, line in facts.middleware_bindings:
        if not (set(names) & facts.registered_names):
            facts.findings.append(
                Finding(
                    severity="high",
                    category="ungoverned-registration",
                    file="",  # filled in by caller, which has rel_path in scope
                    line=line,
                    message=(
                        "build_middleware() result assigned to "
                        f"{', '.join(names)} but never found inside a middleware=[...] "
                        "list in this file -- per maf.py's own docstring, forgetting this "
                        "means zero enforcement, silently."
                    ),
                    recommendation="Pass the assigned middleware into Agent(middleware=[...]).",
                    framework="maf",
                )
            )
    for names, line in facts.plugin_bindings:
        if not (set(names) & facts.registered_names):
            facts.findings.append(
                Finding(
                    severity="high",
                    category="ungoverned-registration",
                    file="",
                    line=line,
                    message=(
                        "build_plugin() result assigned to "
                        f"{', '.join(names)} but never found inside a plugins=[...] list "
                        "(or app.plugins) in this file."
                    ),
                    recommendation="Pass the assigned plugin into Runner(plugins=[...]).",
                    framework="adk",
                )
            )


def _scan_file(path: Path, rel_path: str) -> tuple[list[Finding], str | None, bool]:
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return [], f"{rel_path}: could not read ({exc})", False

    try:
        facts = _scan_source(source, rel_path)
    except SyntaxError as exc:
        return [], f"{rel_path}: syntax error, skipped ({exc.msg} at line {exc.lineno})", False

    _reconcile_registrations(facts)
    findings = []
    for f in facts.findings:
        findings.append(f if f.file else Finding(**{**asdict(f), "file": rel_path}))

    if facts.has_raw_client_ctor and not facts.has_governed_usage:
        for line in facts.has_raw_client_ctor:
            findings.append(
                Finding(
                    severity="medium",
                    category="raw-model-client",
                    file=rel_path,
                    line=line,
                    message=(
                        "Raw model-provider client constructed directly (openai/anthropic/"
                        "google.genai) with no GovernedAgent/GovernedRunner/Governor usage "
                        "found elsewhere in this file -- calls through it bypass Cedar "
                        "entirely. This SDK ships no in-process adapter for a raw client "
                        "used standalone; if this isn't wrapped by a governed agent "
                        "elsewhere, use the framework-neutral Governor around it directly."
                    ),
                    recommendation=(
                        "Wrap calls with parapetai_agent.Governor.check_input()/"
                        "authorize_tool()/check_output(), or route through GovernedAgent/"
                        "GovernedRunner if this client backs one."
                    ),
                )
            )

    return findings, None, facts.uses_parapetai_agent


def _iter_python_files(root: Path) -> list[Path]:
    results: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.endswith(".egg-info"):
                    continue
                stack.append(entry)
            elif entry.suffix == ".py":
                try:
                    if entry.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                results.append(entry)
            if len(results) >= _MAX_FILES_SCANNED:
                return results
    return results


def _scan_dependencies(root: Path) -> tuple[set[str], bool]:
    """Best-effort, root-level only (not recursive -- a monorepo with
    per-package manifests needs auditing per package). Returns
    (frameworks found, parapetai_agent listed as a dependency)."""
    frameworks: set[str] = set()
    has_parapetai = False

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        deps: list[str] = []
        project = data.get("project", {})
        deps.extend(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            deps.extend(group)
        deps_str = " ".join(deps).lower()
        if "agent-framework" in deps_str or "agent_framework" in deps_str:
            frameworks.add("maf")
        if "google-adk" in deps_str:
            frameworks.add("adk")
        if "parapetai-agent" in deps_str:
            has_parapetai = True

    for reqfile in ("requirements.txt", "requirements-dev.txt"):
        req = root / reqfile
        if not req.exists():
            continue
        try:
            text = req.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        if "agent-framework" in text:
            frameworks.add("maf")
        if "google-adk" in text:
            frameworks.add("adk")
        if "parapetai-agent" in text:
            has_parapetai = True

    return frameworks, has_parapetai


def audit_codebase(path: str, output_dir: str | None = None) -> dict[str, Any]:
    root = Path(path).resolve()
    if not root.is_dir():
        return {"error": f"{root} is not a directory"}

    report_dir = Path(output_dir).resolve() if output_dir else root / ".parapet" / "audit"
    report_path = report_dir / "report.md"

    files = _iter_python_files(root)
    truncated = len(files) >= _MAX_FILES_SCANNED

    all_findings: list[Finding] = []
    skipped: list[str] = []
    any_parapetai_import = False

    for f in files:
        rel = f.relative_to(root).as_posix()
        findings, skip_reason, uses_parapetai = _scan_file(f, rel)
        if skip_reason:
            skipped.append(skip_reason)
            continue
        all_findings.extend(findings)
        any_parapetai_import = any_parapetai_import or uses_parapetai

    frameworks, has_parapetai_dep = _scan_dependencies(root)
    if frameworks and not has_parapetai_dep and not any_parapetai_import:
        all_findings.append(
            Finding(
                severity="high",
                category="framework-present-no-governance",
                file=".",
                line=0,
                message=(
                    f"{'/'.join(sorted(frameworks))} dependency detected, but "
                    "parapetai-agent is not a listed dependency and no "
                    "`parapetai_agent` import was found anywhere in the scanned tree -- "
                    "this project has no governance path available at all yet."
                ),
                recommendation=(
                    'pip install "parapetai-agent[maf]" or "parapetai-agent[adk]", '
                    "then see the per-file findings below for specific construction sites."
                ),
            )
        )

    order = {"high": 0, "medium": 1, "low": 2}
    all_findings.sort(key=lambda fnd: (order[fnd.severity], fnd.file, fnd.line))

    summary = {
        "high": sum(1 for f in all_findings if f.severity == "high"),
        "medium": sum(1 for f in all_findings if f.severity == "medium"),
        "low": sum(1 for f in all_findings if f.severity == "low"),
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(root, all_findings, summary, len(files), skipped, truncated),
        encoding="utf-8",
    )

    return {
        "report_path": str(report_path),
        "audited_path": str(root),
        "files_scanned": len(files),
        "files_skipped": skipped,
        "truncated": truncated,
        "summary": summary,
        "findings": [asdict(f) for f in all_findings],
    }


def _render_report(
    root: Path,
    findings: list[Finding],
    summary: dict[str, int],
    files_scanned: int,
    skipped: list[str],
    truncated: bool,
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [
        "# Parapet governance audit",
        "",
        f"- **Audited path**: `{root}`",
        f"- **Generated**: {now}",
        f"- **Files scanned**: {files_scanned}"
        + (" (capped -- more files exist than were scanned)" if truncated else ""),
        f"- **Files skipped**: {len(skipped)}",
        "",
        "This is a **static, best-effort** scan (no control plane call, nothing "
        "sent anywhere) against a curated set of known import/call shapes for "
        "Microsoft Agent Framework, Google ADK, and a handful of raw model "
        "clients. It favors precision over recall: it can miss a real "
        "ungoverned call site it has no pattern for, but should not report a "
        "false HIGH. Absence of a finding is not proof of governance -- treat "
        "this as a starting point for review, not a certification.",
        "",
        "## Summary",
        "",
        "| Severity | Count |",
        "|---|---|",
        f"| 🔴 High | {summary['high']} |",
        f"| 🟡 Medium | {summary['medium']} |",
        f"| 🟢 Low | {summary['low']} |",
        "",
    ]

    if not findings:
        lines.append(
            "No findings. Either every model/tool-call site this scanner knows "
            "how to recognize is already governed, or none were found at all -- "
            "check `files_skipped` and the scanner's known-shape list above "
            "before treating this as a clean bill of health."
        )
    else:
        lines.append("## Findings")
        lines.append("")
        lines.append("| Severity | File:Line | Category | Finding | Recommendation |")
        lines.append("|---|---|---|---|---|")
        icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for f in findings:
            location = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(
                f"| {icon[f.severity]} {f.severity.upper()} | `{location}` | {f.category} "
                f"| {f.message} | {f.recommendation} |"
            )
        lines.append("")

    if skipped:
        lines.append("## Files skipped")
        lines.append("")
        lines.extend(f"- {s}" for s in skipped)
        lines.append("")

    lines.append("## Next step")
    lines.append("")
    lines.append(
        "Run the **parapet-audit-fix** skill (or ask Claude to apply it) to wrap "
        "flagged HIGH/MEDIUM construction sites in `GovernedAgent`/`GovernedRunner` "
        "and re-run this audit to confirm the count went down."
    )
    lines.append("")

    return "\n".join(lines)
