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
    # determining_policies/evaluation_ms come straight off the same Cedar
    # Decision (policy/engine.py) every integration produces -- absent here
    # only for example_no_governance.py's rows, which never call Cedar at
    # all. latency_ms/tokens are wall-clock/usage this driver adds on top,
    # present for both tables (governance overhead is only visible if
    # ungoverned has timing too). See README.md's "Reading the decision
    # data" section.
    print(f"\n=== {title} ===")
    print(
        f"{'name':<8} {'org':<8} {'tool':<20} {'outcome':<10} {'policy':<28} "
        f"{'cedar eval':<12} {'total':<10} {'tokens':<8}"
    )
    for r in results:
        policy = ",".join(r.get("determining_policies") or []) or "-"
        evaluation_ms = r.get("evaluation_ms")
        cedar_eval = f"{evaluation_ms:.3f}ms" if evaluation_ms is not None else "-"
        latency_ms = r.get("latency_ms")
        total = f"{latency_ms:.1f}ms" if latency_ms is not None else "-"
        tokens = r.get("total_tokens")
        tokens_str = str(tokens) if tokens is not None else "-"
        print(
            f"{r['name']:<8} {r['org']:<8} {r['tool']:<20} {r['outcome']:<10} "
            f"{policy:<28} {cedar_eval:<12} {total:<10} {tokens_str:<8}"
        )


def main() -> None:
    print("Running example_no_governance.py ...")
    ungoverned = _run("example_no_governance.py")

    print("\nRunning example_governed.py ...")
    governed = _run("example_governed.py")

    _print_table(f"UNGOVERNED (model: {ungoverned['mode']})", ungoverned["results"])
    _print_table(f"GOVERNED (model: {governed['mode']})", governed["results"])

    print("\n=== Timing & token usage ===")
    ungoverned_ms = [
        r["latency_ms"] for r in ungoverned["results"] if r.get("latency_ms") is not None
    ]
    governed_ms = [
        r["latency_ms"] for r in governed["results"] if r.get("latency_ms") is not None
    ]
    if ungoverned_ms and governed_ms:
        avg_ungoverned = sum(ungoverned_ms) / len(ungoverned_ms)
        avg_governed = sum(governed_ms) / len(governed_ms)
        print(
            f"Average call latency: {avg_ungoverned:.1f}ms ungoverned vs {avg_governed:.1f}ms "
            f"governed ({avg_governed - avg_ungoverned:+.1f}ms/call difference). Only a tiny "
            f"slice of that is Cedar itself -- see the 'cedar eval' column above (sub-millisecond "
            f"here); the rest is model/tool call variance, dominated in mock mode by the first "
            f"call's mock-server startup, not governance."
        )
    governed_tokens = [
        r["total_tokens"] for r in governed["results"] if r.get("total_tokens") is not None
    ]
    if governed["mode"] == "mock":
        print(
            f"Model cost: $0.00 -- mock model, {sum(governed_tokens) if governed_tokens else 0} "
            f"tokens reported are the mock server's canned usage figures, not real spend."
        )
    elif governed_tokens:
        print(
            f"Model cost: not computed -- this SDK has no per-model pricing table -- "
            f"{sum(governed_tokens)} tokens used across {len(governed_tokens)} governed calls; "
            f"check your provider's pricing page for the $ figure."
        )

    print("\n=== Summary ===")
    print(
        "Ungoverned: every call succeeds regardless of org -- Tony can read HR data, "
        "Sally can read Salesforce data. That's the risk."
    )
    print(
        "Governed: the same calls are enforced by Cedar -- each identity only reaches "
        "the tool for their own org. That's Parapet."
    )

    if governed.get("governance_mode") == "local":
        print("\n=== Ran in local mode (PARAPETAI_MODE=local) ===")
        print(
            f"No control plane involved -- Cedar policy was read straight from "
            f"{governed['policy_dir']}. Add/remove .cedar files there to test more rules, "
            f"or `cp .env.cloud .env` to switch back to the real control plane."
        )
    else:
        print("\n=== View the governed agent on the control plane ===")
        print(
            f"org policy, allow/deny decisions, traces: "
            f"{governed['control_plane_url']}/a/{governed['account_id']}/agents/{governed['agent_id']}"
        )


if __name__ == "__main__":
    main()
