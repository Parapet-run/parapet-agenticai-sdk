"""Conformance runner.

Answers exactly one question per framework: with only environment variables
set, does this framework's traffic actually arrive at the gateway?

It does NOT test policy logic — that lives in policies/tests/. Keeping the two
separate matters: a red conformance test means an integration broke, a red
policy test means a rule broke, and conflating them wastes debugging time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "matrix.yaml"

# Verified against Groq's tool-use docs to support standard client-defined
# function calling -- see .env.local.example. Only used when CONFORMANCE_MODEL
# isn't set (e.g. a CI run against the fake upstream, which doesn't care what
# model name it's asked for).
DEFAULT_MODEL = "llama-3.3-70b-versatile"


@dataclass(slots=True)
class Result:
    framework: str
    reached_gateway: bool
    observed_provider: str | None
    observed_action: str | None
    observed_client: str | None = None
    observed_client_version: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.reached_gateway and self.error is None


def load_matrix() -> list[dict[str, Any]]:
    return yaml.safe_load(MATRIX.read_text())["frameworks"]


def run_one(entry: dict[str, Any], gateway: str, token: str, model: str) -> Result:
    """Run a framework probe container and check the gateway saw its traffic."""
    fid = entry["id"]
    probe = ROOT / "frameworks" / fid
    if not probe.exists():
        return Result(fid, False, None, None, error="no probe implemented")

    # Containers can't reach a host-bound gateway via 127.0.0.1/localhost --
    # true on macOS Docker Desktop always, and even `--network host` there
    # shares the Linux VM's loopback, not the Mac's. host.docker.internal
    # works on both Desktop platforms and, via --add-host below, on Linux
    # (Docker 20.10+) too.
    base = gateway.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal"
    )
    # Identity is a base-URL path prefix (see parapetai_agent.identity), not a header --
    # every framework here already sets a base URL, so correlating a run needs
    # zero framework-specific code. A header (the old PARAPETAI_PROBE_MARKER
    # approach) can't say the same: no SDK here exposes a way to set a custom
    # header without constructing a client explicitly.
    agent_id = f"conf-{fid}-{uuid4()}"
    container_gateway = f"{base}/a/{agent_id}"

    env = {
        k: v.replace("{gateway}", container_gateway).replace(
            "{gateway_ws}", container_gateway.replace("http", "ws")
        )
        for k, v in (entry.get("env") or {}).items()
    }
    env["OPENAI_API_KEY"] = token
    env["ANTHROPIC_API_KEY"] = token
    env["GEMINI_API_KEY"] = token
    env["CONFORMANCE_MODEL"] = model

    cmd = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(f"parapetai-probe-{fid}:latest")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)  # noqa: S603
    except subprocess.TimeoutExpired:
        return Result(fid, False, None, None, error="probe timed out")

    observed = _query_observations(gateway, agent_id)
    if not observed:
        return Result(
            fid,
            False,
            None,
            None,
            error=f"no traffic observed; probe exit={proc.returncode} stderr={proc.stderr[-500:]}",
        )

    observed_client = observed.get("client_name")
    observed_client_version = observed.get("client_version")
    declared_client = entry.get("client")
    error = None
    if declared_client and observed_client != declared_client:
        # The point of fingerprinting: matrix.yaml's `client` field is a claim
        # about what actually talks to the gateway on the wire. An integration
        # layer (langchain-openai, CrewAI's "native SDK") can silently wrap a
        # different underlying HTTP client than its name suggests -- catching
        # that here, not assuming it from the package name, is the check.
        error = f"client mismatch: declared={declared_client!r} observed={observed_client!r}"

    return Result(
        fid,
        True,
        observed.get("provider"),
        observed.get("action"),
        observed_client,
        observed_client_version,
        error=error,
    )


def _query_observations(gateway: str, agent_id: str) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            f"{gateway}/__parapetai/observations", params={"agent_id": agent_id}, timeout=10
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        return records[0] if records else None
    except httpx.HTTPError:
        return None


def main() -> int:
    gateway = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    # Forwarded to whatever PARAPETAI_OPENAI_BASE_URL points at, unchanged, under
    # credential passthrough (the gateway default). Real key against Groq for
    # the nightly live run; the fake upstream (CI default) doesn't check it.
    token = os.environ.get("GROQ_API_KEY") or "conformance-token"  # noqa: S105
    model = os.environ.get("CONFORMANCE_MODEL", DEFAULT_MODEL)
    results = [run_one(e, gateway, token, model) for e in load_matrix()]

    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        detail = (
            f"provider={r.observed_provider} action={r.observed_action} "
            f"client={r.observed_client}@{r.observed_client_version}"
            if r.passed
            else (r.error or "")
        )
        print(f"[{flag}] {r.framework:<16} {detail}")

    (ROOT / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
