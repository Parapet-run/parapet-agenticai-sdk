"""What the gateway reports to the control plane about itself.

The report is the operator's only window into a gateway's certificates,
rotations and connected agents, so the tests pin the properties that make it
trustworthy: it is content-free, bounded against a hostile caller, and it
describes what is actually LOADED, never what is merely on disk.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import parapetai_gateway.server.app as app_module
import pytest
from fastapi.testclient import TestClient
from parapetai_gateway.identity.bindings import Binding, BindingTable
from parapetai_gateway.identity.jwks import StaticKeyProvider
from parapetai_gateway.identity.resolver import IdentityResolver, IdpConfig, JwtVerifier
from parapetai_gateway.server.app import create_app
from parapetai_gateway.status import GatewayStatus, describe_certificates
from parapetai_gateway.tls_reload import TlsFiles, TlsReloader

from parapetai_agent.policy.engine import PolicyEngine

from .identity_support import (
    AUDIENCE,
    ISSUER,
    issue_server_cert,
    make_ca,
    make_token,
    new_rsa_key,
)

POLICIES = Path(__file__).resolve().parents[2] / "policies"
KEY = new_rsa_key()


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


# ── certificate metadata ─────────────────────────────────────────────────


def test_a_combined_pem_yields_certificate_metadata_and_never_the_private_key(
    tmp_path: Path,
) -> None:
    ca = make_ca(tmp_path, "the-ca")
    crt, key = issue_server_cert(ca, tmp_path)
    combined = key.read_bytes() + b"\n" + crt.read_bytes()  # key first, as a vault exports it

    described = describe_certificates(combined)

    assert len(described) == 1
    cert = described[0]
    assert cert["subject"] == "localhost"
    assert cert["issuer"] == "the-ca"
    assert len(cert["sha256"]) == 64 and cert["not_after"] > cert["not_before"]
    rendered = json.dumps(described)
    assert "PRIVATE" not in rendered
    assert key.read_bytes().split(b"\n")[1].decode() not in rendered  # no key material at all


def test_every_certificate_in_a_bundle_is_described(tmp_path: Path) -> None:
    a, b = make_ca(tmp_path, "ca-a"), make_ca(tmp_path, "ca-b")

    described = describe_certificates(a.cert_path.read_bytes() + b.cert_path.read_bytes())

    assert [d["subject"] for d in described] == ["ca-a", "ca-b"]


@pytest.mark.parametrize(
    "junk",
    [b"", b"not pem at all", b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"],
)
def test_unparseable_certificates_are_skipped_not_fatal(junk: bytes) -> None:
    assert describe_certificates(junk) == []


# ── recording ────────────────────────────────────────────────────────────


def test_requests_are_counted_per_identity_with_denials_and_holds() -> None:
    status = GatewayStatus(clock=_Clock())

    for effect in ("allow", "allow", "deny", "review"):
        status.record_request(
            agent_id="fib-sales", method="mtls", subject="fib-sales", effect=effect
        )

    (row,) = status.snapshot()["connected"]
    assert (row["requests"], row["denied"], row["held_for_review"]) == (4, 1, 1)
    assert row["agent_id"] == "fib-sales" and row["method"] == "mtls"


def test_the_same_agent_over_two_methods_is_two_rows() -> None:
    status = GatewayStatus(clock=_Clock())

    status.record_request(agent_id="a", method="mtls", subject="a", effect="allow")
    status.record_request(agent_id="a", method="path", subject=None, effect="allow")

    assert {r["method"] for r in status.snapshot()["connected"]} == {"mtls", "path"}


def test_only_identities_seen_within_the_window_count_as_connected() -> None:
    clock = _Clock()
    status = GatewayStatus(window_s=600, clock=clock)
    status.record_request(agent_id="old", method="mtls", subject="old", effect="allow")
    clock.now += 601
    status.record_request(agent_id="new", method="mtls", subject="new", effect="allow")

    assert [r["agent_id"] for r in status.snapshot()["connected"]] == ["new"]


def test_the_most_recently_seen_identity_is_listed_first() -> None:
    clock = _Clock()
    status = GatewayStatus(clock=clock)
    for name in ("a", "b", "c"):
        clock.now += 1
        status.record_request(agent_id=name, method="mtls", subject=name, effect="allow")

    assert [r["agent_id"] for r in status.snapshot()["connected"]] == ["c", "b", "a"]


def test_a_flood_of_made_up_agent_ids_cannot_grow_memory_without_limit() -> None:
    """The /a/{agent_id} path claim is attacker-controlled."""
    status = GatewayStatus(max_identities=100, clock=_Clock())

    for i in range(5000):
        status.record_request(agent_id=f"spoofed-{i}", method="path", subject=None, effect="allow")
    status.record_request(agent_id="legit", method="mtls", subject="legit", effect="allow")

    connected = status.snapshot()["connected"]
    assert len(connected) <= 100
    assert connected[0]["agent_id"] == "legit"  # the recent one survives; the oldest were evicted


def test_an_overlong_identifier_is_clipped() -> None:
    status = GatewayStatus(clock=_Clock())

    status.record_request(
        agent_id="x" * 10_000, method="path", subject="y" * 10_000, effect="allow"
    )

    (row,) = status.snapshot()["connected"]
    assert len(row["agent_id"]) <= 128 and len(row["subject"]) <= 128


def test_refused_credentials_are_counted_by_reason_and_never_identify_anyone() -> None:
    status = GatewayStatus(clock=_Clock())

    for code in ("invalid_token", "invalid_token", "identity_not_bound"):
        status.record_identity_refusal(code)
    for i in range(200):
        status.record_identity_refusal(f"made-up-{i}")

    report = status.snapshot()
    assert report["identity_refusals"]["invalid_token"] == 2
    assert len(report["identity_refusals"]) <= 32  # distinct reasons are bounded too
    assert report["connected"] == []


def test_recent_events_are_a_bounded_ring() -> None:
    status = GatewayStatus(max_events=5, clock=_Clock())

    for i in range(50):
        status.add_event("tls_reloaded", generation=i)

    events = status.snapshot()["recent_events"]
    assert len(events) == 5 and events[-1]["generation"] == 49


# ── the report itself ────────────────────────────────────────────────────


def test_the_report_is_json_serialisable_and_labelled() -> None:
    status = GatewayStatus(site="dc-1", clock=_Clock())
    status.describe_config(mtls=True, identity_required=True, mode="enforce")

    report = json.loads(json.dumps(status.snapshot()))

    assert report["kind"] == "gateway" and report["schema"] == 1
    assert report["site"] == "dc-1"
    assert report["config"] == {"mtls": True, "identity_required": True, "mode": "enforce"}
    assert "tls" not in report  # no TLS files watched -> no TLS block


def test_the_report_carries_no_content_shaped_keys() -> None:
    status = GatewayStatus(clock=_Clock())
    status.record_request(agent_id="a", method="mtls", subject="a", effect="deny")
    status.add_event("tls_reloaded", generation=1)

    rendered = json.dumps(status.snapshot()).lower()

    for forbidden in (
        "prompt",
        "response",
        "token",
        "secret",
        "password",
        "private",
        "tool_args",
        "arguments",
    ):
        assert forbidden not in rendered, forbidden


# ── TLS state describes what is LOADED ───────────────────────────────────


def _tls_files(tmp_path: Path, name: str) -> tuple[TlsFiles, Any]:
    directory = tmp_path / name
    directory.mkdir()
    srv, cli = make_ca(directory, f"{name}-srv"), make_ca(directory, f"{name}-cli")
    crt, key = issue_server_cert(srv, directory)
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    files = TlsFiles(live / "server.crt", live / "server.key", live / "ca.crt", "required")
    return files, (crt, key, cli.cert_path)


def _install(files: TlsFiles, material: Any) -> None:
    crt, key, ca = material
    assert files.key is not None
    files.cert.write_bytes(crt.read_bytes())
    files.key.write_bytes(key.read_bytes())
    files.client_ca.write_bytes(ca.read_bytes())


def test_a_reload_updates_the_reported_certificate_and_records_the_rotation(tmp_path: Path) -> None:
    files, first = _tls_files(tmp_path, "one")
    _, second = _tls_files(tmp_path, "two")
    _install(files, first)
    status = GatewayStatus(clock=_Clock())
    status.watch_tls(files.cert, files.key, files.client_ca)
    before = status.snapshot()["tls"]["server_cert"]["sha256"]

    _install(files, second)
    reloader = TlsReloader(files, on_event=status.on_tls_event)
    reloader._digest = "stale"  # the reloader last loaded something else
    assert reloader.check() is True

    tls = status.snapshot()["tls"]
    assert tls["server_cert"]["sha256"] != before
    assert tls["generation"] == 1 and tls["last_error"] is None
    assert tls["client_ca"][0]["subject"] == "two-cli"
    assert status.snapshot()["recent_events"][-1]["type"] == "tls_reloaded"


def test_a_rejected_rotation_reports_the_error_but_still_the_material_that_is_loaded(
    tmp_path: Path,
) -> None:
    """Valid certificates paired with the WRONG key fail to load, but a naive
    re-read of the files would report the rejected certificate as being served."""
    files, first = _tls_files(tmp_path, "one")
    _, second = _tls_files(tmp_path, "two")
    _install(files, first)
    status = GatewayStatus(clock=_Clock())
    status.watch_tls(files.cert, files.key, files.client_ca)
    loaded = status.snapshot()["tls"]["server_cert"]["sha256"]
    reloader = TlsReloader(files, on_event=status.on_tls_event)

    assert files.key is not None
    files.cert.write_bytes(second[0].read_bytes())  # a valid cert...
    files.key.write_bytes(first[1].read_bytes())  # ...with the previous key: does not match
    assert reloader.check() is False

    tls = status.snapshot()["tls"]
    assert tls["server_cert"]["sha256"] == loaded  # what is served, not what is on disk
    assert tls["last_error"]
    assert status.snapshot()["recent_events"][-1]["type"] == "tls_reload_failed"


def test_a_broken_event_callback_never_affects_the_reload(tmp_path: Path) -> None:
    files, first = _tls_files(tmp_path, "one")
    _, second = _tls_files(tmp_path, "two")
    _install(files, first)

    def boom(event_type: str, details: dict[str, Any]) -> None:
        raise RuntimeError("status is broken")

    reloader = TlsReloader(files, on_event=boom)
    _install(files, second)

    assert reloader.check() is True  # still reloaded
    assert reloader.generation == 1


# ── recorded through the running app ─────────────────────────────────────


def _app(
    monkeypatch: pytest.MonkeyPatch, status: GatewayStatus | None, *, jwt: bool = False
) -> TestClient:
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings))
    resolver = None
    if jwt:
        resolver = IdentityResolver(
            bindings=BindingTable([Binding("jwt", "agent-a", issuer=ISSUER, subject="app-1")]),
            jwt_verifier=JwtVerifier(
                IdpConfig(issuer=ISSUER, audiences=(AUDIENCE,)),
                StaticKeyProvider({"k1": KEY.public_key()}),
            ),
        )
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine, identity_resolver=resolver, status=status))


_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "execute_shell"}}


def test_the_gateway_records_each_verified_identity_that_calls_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = GatewayStatus(clock=_Clock())
    client = _app(monkeypatch, status, jwt=True)

    for _ in range(3):
        client.post("/mcp", json=_CALL, headers={"x-parapetai-identity": make_token(KEY)})

    (row,) = status.snapshot()["connected"]
    assert (row["agent_id"], row["method"], row["subject"]) == ("agent-a", "jwt", "app-1")
    assert (row["requests"], row["denied"]) == (3, 3)  # execute_shell is denied by policy


def test_a_refused_credential_is_counted_and_records_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = GatewayStatus(clock=_Clock())
    client = _app(monkeypatch, status, jwt=True)

    client.post("/mcp", json=_CALL, headers={"x-parapetai-identity": "garbage"})

    report = status.snapshot()
    assert report["identity_refusals"] == {"invalid_token": 1}
    assert report["connected"] == []


def test_an_unverified_path_claim_is_recorded_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    status = GatewayStatus(clock=_Clock())
    client = _app(monkeypatch, status)

    client.post("/a/some-agent/mcp", json=_CALL)

    (row,) = status.snapshot()["connected"]
    assert (row["agent_id"], row["method"], row["subject"]) == ("some-agent", "path", None)


def test_the_gateway_works_without_a_status_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(monkeypatch, None)

    assert client.post("/mcp", json=_CALL).status_code == 403
