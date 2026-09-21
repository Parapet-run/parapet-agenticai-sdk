"""The base install (no extras) must import, enforce, and degrade cleanly.

`parapetai-agent` depends on opentelemetry-api only; the OpenTelemetry SDK and OTLP
exporter live in extras (`otel`, `maf`, `adk`, `langgraph`). For a while
`governance_runtime` imported the SDK at module level, and `import parapetai_agent`
imports it, so a bare `pip install parapetai-agent` could not even be imported:

    ModuleNotFoundError: No module named 'opentelemetry.sdk'

Nothing in the suite noticed, because the dev environment always has every extra.
These tests pin the contract in three ways, and `make smoke-base` (run in CI) checks
it in a genuinely clean virtualenv:

  * a subprocess that BLOCKS the SDK from importing still imports, enforces policy,
    and fails with an actionable message only when telemetry export is requested;
  * the control-plane embed path keeps enforcing when the SDK is missing, rather
    than crashing (enforcement must never depend on telemetry);
  * a static check that no module imports the SDK at module level.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import respx
from httpx import Response

import parapetai_agent
from parapetai_agent import GovernanceDenied, Governor

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(parapetai_agent.__file__).resolve().parent
POLICIES = ROOT / "policies"

# Makes `import opentelemetry.sdk...` / `opentelemetry.exporter...` fail exactly as it
# does in an environment that only has opentelemetry-api.
_BLOCK_SDK = textwrap.dedent(
    """
    import importlib.abc, sys

    class _NoSdk(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "opentelemetry.sdk" or name.startswith(
                ("opentelemetry.sdk.", "opentelemetry.exporter")
            ):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    sys.meta_path.insert(0, _NoSdk())
    """
)


def _run_without_the_sdk(body: str) -> dict[str, object]:
    script = _BLOCK_SDK + textwrap.dedent(body)
    result = subprocess.run(  # noqa: S603 -- our own interpreter and script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_the_blocker_really_blocks_the_sdk() -> None:
    """Guards the other tests against passing vacuously."""
    out = _run_without_the_sdk(
        """
        import json
        try:
            import opentelemetry.sdk.trace
            print(json.dumps({"blocked": False}))
        except ImportError:
            print(json.dumps({"blocked": True}))
        """
    )

    assert out["blocked"] is True


def test_the_package_imports_and_enforces_policy_without_the_sdk() -> None:
    out = _run_without_the_sdk(
        f"""
        import json
        import parapetai_agent
        from parapetai_agent import Governor

        gov = Governor.from_policy_dir({str(POLICIES)!r}, {str(POLICIES / "entities.json")!r})
        allowed = gov.authorize_tool("lookup_order", {{"order_id": "A1"}}, raise_on_deny=False)
        denied = gov.authorize_tool("execute_shell", {{"cmd": "ls"}}, raise_on_deny=False)
        print(json.dumps({{"allowed": allowed.allowed, "denied_effect": denied.effect}}))
        """
    )

    assert out == {"allowed": True, "denied_effect": "deny"}


def test_requesting_telemetry_export_without_the_sdk_says_what_to_install() -> None:
    out = _run_without_the_sdk(
        """
        import json
        from parapetai_agent.governance_runtime import configure_otel

        try:
            configure_otel(console=False)
            print(json.dumps({"raised": False}))
        except ImportError as exc:
            print(json.dumps({"raised": True, "message": str(exc)}))
        """
    )

    assert out["raised"] is True
    assert "parapetai-agent[otel]" in str(out["message"])


# ── the control-plane embed path degrades instead of crashing ─────────────

CP = "https://cp.example"


def _mock_control_plane() -> None:
    respx.post(f"{CP}/api/v1/keys").mock(return_value=Response(200, json={"status": "ok"}))
    respx.post(f"{CP}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    respx.get(f"{CP}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "files": {
                    "00-base.cedar": (
                        'permit (principal, action == Action::"tool_call", resource);\n'
                        'forbid (principal, action == Action::"tool_call", resource)\n'
                        "when { context has tool_name && "
                        'context.tool_name == "delete_everything" };'
                    ),
                    "entities.json": "[]",
                },
            },
        )
    )


@respx.mock
def test_from_control_plane_keeps_enforcing_when_the_sdk_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry export needs the SDK; enforcement does not. A base install with a
    control plane configured must still pull policy and block, and must say plainly
    what is missing instead of failing to start.

    The log event is captured with structlog's own capture, NOT pytest's `capsys`:
    building a Governor from a control plane lazily imports `mcp`, whose stdio client
    binds `errlog=sys.stderr` at import time. Under `capsys` that is a stream with no
    fileno, which then breaks every later MCP stdio test in the run."""
    from structlog.testing import capture_logs

    import parapetai_agent.control_plane as cp
    import parapetai_agent.govern as govern

    def _no_sdk(**_: object) -> None:
        raise ImportError("No module named 'opentelemetry.sdk'")

    monkeypatch.setattr(govern, "_otel_configured", lambda: False)
    monkeypatch.setattr(govern, "_configure_otel", _no_sdk)
    monkeypatch.setattr(cp, "run_bundle_poller", lambda *a, **k: None)
    for var in ("PARAPETAI_CONTROL_PLANE_URL", "PARAPETAI_AGENT_SECRET", "PARAPETAI_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)
    _mock_control_plane()
    local = tmp_path / "local"
    local.mkdir()
    (local / "00-base.cedar").write_text("permit (principal, action, resource);")

    with capture_logs() as logs:
        gov = Governor.from_control_plane(CP, "secret", policy_dir=local)
    try:
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_everything", {})
        assert gov.authorize_tool("read_thing", {}).allowed is True
    finally:
        gov.stop_sync()

    warned = [e for e in logs if e["event"] == "otel_sdk_missing_telemetry_export_disabled"]
    assert warned and "parapetai-agent[otel]" in warned[0]["hint"]


# ── nothing imports the SDK at module level ──────────────────────────────

_SDK_PREFIXES = ("opentelemetry.sdk", "opentelemetry.exporter", "opentelemetry.instrumentation")


def _module_level_sdk_imports(path: Path) -> list[str]:
    """Imports of the OTel SDK that run the moment the module is imported. Imports
    inside a function, an `if TYPE_CHECKING:` block or a `try/except ImportError`
    are fine: they only run when needed, or are guarded."""
    offenders = []
    for node in ast.parse(path.read_text()).body:
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        offenders += [
            f"{path.name}:{node.lineno}: {n}" for n in names if n.startswith(_SDK_PREFIXES)
        ]
    return offenders


def test_no_module_imports_the_opentelemetry_sdk_at_module_level() -> None:
    offenders = [o for path in sorted(SRC.rglob("*.py")) for o in _module_level_sdk_imports(path)]

    assert not offenders, (
        "the base install has opentelemetry-api only; importing the SDK at module level "
        "makes `import parapetai_agent` fail without an extra:\n" + "\n".join(offenders)
    )


def test_the_static_check_actually_catches_a_module_level_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from opentelemetry.sdk.trace import TracerProvider\n")
    fine = tmp_path / "fine.py"
    fine.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from opentelemetry.sdk.trace import TracerProvider\n"
        "try:\n    from opentelemetry.sdk.trace import SpanProcessor\n"
        "except ImportError:\n    SpanProcessor = object\n"
        "def f():\n    from opentelemetry.sdk.trace import TracerProvider\n"
    )

    assert _module_level_sdk_imports(bad)
    assert not _module_level_sdk_imports(fine)
