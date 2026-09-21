"""Run by `make smoke-base` inside a virtualenv holding ONLY the built wheel and its
base dependencies (no extras). Not a pytest module: it must run where pytest, and every
optional dependency, are absent.

It asserts the base-install contract: the package imports, enforces policy, and asks for
the `otel` extra (rather than crashing at import) only when telemetry export is requested.
"""

from __future__ import annotations

import sys
from pathlib import Path

policies = Path(sys.argv[1])

import parapetai_agent  # noqa: E402,F401 -- the import IS the first assertion
from parapetai_agent import Governor  # noqa: E402
from parapetai_agent.governance_runtime import configure_otel  # noqa: E402

gov = Governor.from_policy_dir(policies, policies / "entities.json")
allowed = gov.authorize_tool("lookup_order", {"order_id": "A1"}, raise_on_deny=False)
denied = gov.authorize_tool("execute_shell", {"cmd": "ls"}, raise_on_deny=False)
assert allowed.allowed is True, allowed
assert denied.effect == "deny", denied

try:
    configure_otel(console=False)
except ImportError as exc:
    assert "parapetai-agent[otel]" in str(exc), exc
else:
    raise SystemExit("configure_otel() succeeded, so the OpenTelemetry SDK was installed: "
                     "this venv is not a base install")

print("base install OK: imports, enforces policy, and asks for [otel] only for telemetry export")
