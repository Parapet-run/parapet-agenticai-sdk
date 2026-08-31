"""Unit tests for parapetai_mcp.audit -- the AST-based governance scanner
behind parapet_audit_codebase. _scan_source is used directly for the
finding-shape tests (no filesystem needed); audit_codebase is exercised
end to end via tmp_path for the whole-directory/report-writing behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from parapetai_mcp.audit import Finding, _reconcile_registrations, _scan_source, audit_codebase


def _findings(source: str, category: str | None = None) -> list[Finding]:
    facts = _scan_source(source, "test.py")
    _reconcile_registrations(facts)
    if category is None:
        return facts.findings
    return [f for f in facts.findings if f.category == category]


def test_raw_maf_agent_with_literal_tools_is_high() -> None:
    source = """
from agent_framework import Agent

agent = Agent(client=c, name="x", instructions="y", tools=[lookup_order])
"""
    findings = _findings(source, "ungoverned-tool-call")
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].framework == "maf"


def test_raw_maf_agent_with_destructive_tool_name_flags_it() -> None:
    source = """
from agent_framework import Agent

agent = Agent(client=c, name="x", instructions="y", tools=[delete_account])
"""
    findings = _findings(source, "ungoverned-tool-call")
    assert len(findings) == 1
    assert "delete_account" in findings[0].message


def test_raw_maf_agent_no_tools_is_medium() -> None:
    source = """
from agent_framework import Agent

agent = Agent(client=c, name="x", instructions="y")
"""
    findings = _findings(source, "ungoverned-model-call")
    assert len(findings) == 1
    assert findings[0].severity == "medium"


def test_governed_agent_with_policy_dir_is_low() -> None:
    source = """
from parapetai_agent import GovernedAgent

agent = GovernedAgent(client=c, name="x", instructions="y", policy_dir="./policies")
"""
    findings = _findings(source, "governed")
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].framework == "maf"


def test_governed_agent_with_no_policy_source_is_medium() -> None:
    source = """
from parapetai_agent import GovernedAgent

agent = GovernedAgent(client=c, name="x", instructions="y")
"""
    findings = _findings(source, "generic-policy-only")
    assert len(findings) == 1
    assert findings[0].severity == "medium"


def test_aliased_governed_import_is_recognized_as_governed_not_raw() -> None:
    # This is the exact pattern the parapet-maf skill teaches: `Agent` is
    # locally bound to GovernedAgent, so a call site that reads Agent(...)
    # must resolve as GOVERNED, not raw -- the whole point of resolving
    # through the import-alias map rather than matching on local names.
    source = """
from parapetai_agent import GovernedAgent as Agent

agent = Agent(client=c, name="x", instructions="y", policy_dir="./policies")
"""
    facts_findings = _findings(source)
    assert not any(
        f.category in ("ungoverned-tool-call", "ungoverned-model-call") for f in facts_findings
    )
    assert any(f.category == "governed" for f in facts_findings)


def test_raw_adk_runner_and_in_memory_runner_detected() -> None:
    source = """
from google.adk.runners import Runner, InMemoryRunner

r1 = Runner(agent=a, app_name="x", session_service=s)
r2 = InMemoryRunner(agent=a)
"""
    findings = _findings(source, "ungoverned-model-call")
    assert len(findings) == 2
    assert all(f.framework == "adk" for f in findings)


def test_build_middleware_registered_in_middleware_list_is_clean() -> None:
    source = """
from parapetai_agent.maf import build_middleware
from agent_framework import Agent

chat_mw, func_mw = build_middleware(policy_dir="./policies")
agent = Agent(client=c, name="x", instructions="y", middleware=[chat_mw, func_mw])
"""
    findings = _findings(source, "ungoverned-registration")
    assert findings == []


def test_build_middleware_assigned_but_never_registered_is_high() -> None:
    source = """
from parapetai_agent.maf import build_middleware

chat_mw, func_mw = build_middleware(policy_dir="./policies")
"""
    findings = _findings(source, "ungoverned-registration")
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].framework == "maf"


def test_build_middleware_result_discarded_bare_is_high() -> None:
    source = """
from parapetai_agent.maf import build_middleware

build_middleware(policy_dir="./policies")
"""
    findings = _findings(source, "ungoverned-registration")
    assert len(findings) == 1
    assert "discarded" in findings[0].message


def test_build_plugin_never_registered_is_high_with_adk_framework() -> None:
    source = """
from parapetai_agent.adk import build_plugin

plugin = build_plugin(policy_dir="./policies")
"""
    findings = _findings(source, "ungoverned-registration")
    assert len(findings) == 1
    assert findings[0].framework == "adk"


def test_raw_openai_client_with_no_governance_is_medium(tmp_path: Path) -> None:
    # raw-model-client findings are assembled in _scan_file (which cross-
    # checks facts.has_raw_client_ctor against facts.has_governed_usage
    # across the whole file), not inside _scan_source itself -- so this
    # needs the real file-scanning path, not the _findings() unit helper.
    (tmp_path / "app.py").write_text(
        "from openai import OpenAI\n\nclient = OpenAI()\n", encoding="utf-8"
    )
    result = audit_codebase(str(tmp_path))
    findings = [f for f in result["findings"] if f["category"] == "raw-model-client"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "medium"


def test_raw_openai_client_suppressed_when_governed_usage_present(tmp_path: Path) -> None:
    # The raw client backs a GovernedAgent elsewhere in the same file --
    # not itself a finding-worthy shape.
    (tmp_path / "app.py").write_text(
        "from openai import OpenAI\n"
        "from parapetai_agent import GovernedAgent\n\n"
        "client = OpenAI()\n"
        "agent = GovernedAgent(\n"
        "    client=client, name='x', instructions='y', policy_dir='./policies'\n"
        ")\n",
        encoding="utf-8",
    )
    result = audit_codebase(str(tmp_path))
    findings = [f for f in result["findings"] if f["category"] == "raw-model-client"]
    assert findings == []


def test_dynamic_tools_value_reported_honestly_not_enumerated() -> None:
    source = """
from agent_framework import Agent

agent = Agent(client=c, name="x", instructions="y", tools=some_variable)
"""
    findings = _findings(source, "ungoverned-tool-call")
    assert len(findings) == 1
    assert "non-literal" in findings[0].message


def test_syntax_error_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (tmp_path / "fine.py").write_text(
        "from agent_framework import Agent\nagent = Agent(client=c, name='x', instructions='y')\n",
        encoding="utf-8",
    )
    result = audit_codebase(str(tmp_path))
    assert result["files_scanned"] == 2
    assert len(result["files_skipped"]) == 1
    assert "broken.py" in result["files_skipped"][0]
    assert result["summary"]["medium"] == 1


def test_audit_codebase_writes_report_and_returns_matching_summary(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from agent_framework import Agent\n"
        "agent = Agent(client=c, name='x', instructions='y', tools=[delete_all])\n",
        encoding="utf-8",
    )
    result = audit_codebase(str(tmp_path))
    report_path = Path(result["report_path"])
    assert report_path.exists()
    assert report_path == tmp_path / ".parapet" / "audit" / "report.md"
    content = report_path.read_text(encoding="utf-8")
    assert "app.py:2" in content
    assert result["summary"]["high"] == 1
    assert len(result["findings"]) == 1


def test_audit_codebase_custom_output_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "from agent_framework import Agent\nagent = Agent(client=c, name='x', instructions='y')\n",
        encoding="utf-8",
    )
    out = tmp_path / "reports"
    result = audit_codebase(str(src), output_dir=str(out))
    assert Path(result["report_path"]) == out / "report.md"
    assert (out / "report.md").exists()


def test_audit_codebase_rejects_non_directory(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    result = audit_codebase(str(f))
    assert "error" in result


def test_venv_and_git_directories_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text(
        "from agent_framework import Agent\nagent = Agent(client=c, name='x', instructions='y')\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = audit_codebase(str(tmp_path))
    assert result["files_scanned"] == 1
    assert result["findings"] == []


def test_framework_dependency_present_without_parapetai_agent_is_high(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["agent-framework>=1.0"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = audit_codebase(str(tmp_path))
    categories = {f["category"] for f in result["findings"]}
    assert "framework-present-no-governance" in categories


def test_framework_dependency_present_with_parapetai_agent_installed_is_clean(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["agent-framework>=1.0", "parapetai-agent[maf]"]\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = audit_codebase(str(tmp_path))
    categories = {f["category"] for f in result["findings"]}
    assert "framework-present-no-governance" not in categories


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
