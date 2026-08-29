"""Runs example_no_governance.py then example_governed.py as two separate
subprocesses (each configures structlog/dotenv globally for itself --
running them in-process would fight over that), parses the
QUICKDEMO_RESULT_JSON: line each prints on its last line of stdout, and
renders one side-by-side table plus the governed agent's control-plane
link. Only the governed agent is provisioned -- example_no_governance.py
never calls the control plane, so a second agent would have nothing to
show.

Usage:
    uv run python driver.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _run(script: str) -> dict:
    proc = subprocess.run(  # noqa: S603 -- fixed local script name, not untrusted input
        [sys.executable, str(HERE / script)],
        capture_output=True,
        text=True,
        cwd=HERE,
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{script} exited {proc.returncode}")
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("QUICKDEMO_RESULT_JSON:"):
            return json.loads(line[len("QUICKDEMO_RESULT_JSON:") :])
    raise SystemExit(f"{script} did not print a QUICKDEMO_RESULT_JSON line")


def _print_table(title: str, results: list[dict]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'name':<8} {'org':<8} {'tool':<20} {'outcome':<10}")
    for r in results:
        print(f"{r['name']:<8} {r['org']:<8} {r['tool']:<20} {r['outcome']:<10}")


def main() -> None:
    print("Running example_no_governance.py ...")
    ungoverned = _run("example_no_governance.py")

    print("\nRunning example_governed.py ...")
    governed = _run("example_governed.py")

    _print_table(f"UNGOVERNED (model: {ungoverned['mode']})", ungoverned["results"])
    _print_table(f"GOVERNED (model: {governed['mode']})", governed["results"])

    print("\n=== Summary ===")
    print(
        "Ungoverned: every call succeeds regardless of org -- Tony can read HR data, "
        "Sally can read Salesforce data. That's the risk."
    )
    print(
        "Governed: the same calls are enforced by Cedar -- each identity only reaches "
        "the tool for their own org. That's Parapet."
    )

    print("\n=== View the governed agent on the control plane ===")
    print(
        f"org policy, allow/deny decisions, traces: "
        f"{governed['control_plane_url']}/a/{governed['account_id']}/agents/{governed['agent_id']}"
    )


if __name__ == "__main__":
    main()
