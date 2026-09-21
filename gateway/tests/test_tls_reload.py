"""Hot reload of mTLS material, against a real TLS server built by the same
`build_server` production uses.

The properties, each pinned below:

* rotating the SERVER certificate takes effect for new connections, no restart;
* rotating the CLIENT CA takes effect: the old CA's clients are refused at the
  handshake, the new CA's are accepted;
* it works for clients that send no SNI (a connection by IP);
* a bad rotation never empties trust or stops the listener -- the previous
  material keeps serving, and a later good rotation recovers;
* unusable material at STARTUP fails closed.
"""

from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from parapetai_gateway.identity.bindings import Binding, BindingTable
from parapetai_gateway.identity.resolver import IdentityResolver
from parapetai_gateway.server.app import create_app
from parapetai_gateway.server.serve import build_server
from parapetai_gateway.tls_reload import TlsFiles, TlsReloader, build_context

from parapetai_agent.policy.engine import PolicyEngine

from .identity_support import Ca, issue_client_cert, issue_server_cert, make_ca

POLICIES = Path(__file__).resolve().parents[2] / "policies"
_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "execute_shell"},  # denied by policy: reaching a decision proves identity
}


@dataclass
class Generation:
    """One issuance of everything the gateway reads: a server certificate (and
    the CA clients must trust to accept it) and the CA it trusts for clients."""

    server_ca: Ca
    client_ca: Ca
    server_crt: Path
    server_key: Path

    def client(self, cn: str = "fib-sales") -> tuple[Path, Path]:
        return issue_client_cert(self.client_ca, self.server_crt.parent, cn, name=f"c-{cn}")


def _generation(root: Path, name: str) -> Generation:
    directory = root / name
    directory.mkdir()
    server_ca = make_ca(directory, f"{name}-server-ca")
    client_ca = make_ca(directory, f"{name}-client-ca")
    crt, key = issue_server_cert(server_ca, directory)
    return Generation(server_ca, client_ca, crt, key)


@pytest.fixture
def pki(tmp_path: Path) -> tuple[Generation, Generation]:
    return _generation(tmp_path, "one"), _generation(tmp_path, "two")


@pytest.fixture
def live(tmp_path: Path, pki: tuple[Generation, Generation]) -> TlsFiles:
    """The directory the gateway reads, initially holding generation one."""
    directory = tmp_path / "live"
    directory.mkdir()
    files = TlsFiles(
        cert=directory / "server.crt",
        key=directory / "server.key",
        client_ca=directory / "client-ca.crt",
        client_auth="required",
    )
    _install(files, pki[0])
    return files


def _install(files: TlsFiles, gen: Generation) -> None:
    assert files.key is not None
    files.cert.write_bytes(gen.server_crt.read_bytes())
    files.key.write_bytes(gen.server_key.read_bytes())
    files.client_ca.write_bytes(gen.client_ca.cert_path.read_bytes())


def _app() -> Any:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    resolver = IdentityResolver(
        bindings=BindingTable([Binding("mtls", "agent-mtls", cn="fib-sales")]),
        mtls_enabled=True,
    )
    return create_app(engine, identity_resolver=resolver)


@contextmanager
def _serving(files: TlsFiles, *, interval_s: float = 0.0) -> Iterator[tuple[int, TlsReloader]]:
    server, reloader = build_server(
        _app(),
        host="127.0.0.1",
        port=0,
        tls=files,
        tls_reload_interval_s=interval_s,
        log_level="error",
    )
    assert reloader is not None
    reloader.start()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "test server did not start"
        time.sleep(0.02)
    try:
        yield server.servers[0].sockets[0].getsockname()[1], reloader
    finally:
        reloader.stop()
        server.should_exit = True
        thread.join(timeout=10)


def _client(gen: Generation, *, present: tuple[Path, Path] | None) -> httpx.Client:
    """Trusts `gen`'s server CA and (optionally) presents a client certificate."""
    context = ssl.create_default_context(cafile=str(gen.server_ca.cert_path))
    if present:
        context.load_cert_chain(str(present[0]), str(present[1]))
    return httpx.Client(verify=context, timeout=5)


def _reaches_a_decision(client: httpx.Client, port: int, host: str = "localhost") -> bool:
    """True if the request got past TLS AND identity to a Cedar decision."""
    resp = client.post(f"https://{host}:{port}/mcp", json=_BODY)
    return "x-parapetai-decision" in resp.headers


def _refused(client: httpx.Client, port: int, host: str = "localhost") -> bool:
    try:
        client.post(f"https://{host}:{port}/mcp", json=_BODY)
    except httpx.TransportError:
        return True
    return False


# ── server certificate rotation ──────────────────────────────────────────


def test_a_rotated_server_certificate_is_served_without_a_restart(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    one, two = pki
    with _serving(live) as (port, reloader):
        with _client(one, present=one.client()) as before:
            assert _reaches_a_decision(before, port)
        with _client(two, present=two.client()) as not_yet:
            assert _refused(not_yet, port)  # generation two's server CA is not what is served yet

        _install(live, two)
        assert reloader.check() is True

        with _client(two, present=two.client()) as after:
            assert _reaches_a_decision(after, port)
        with _client(one, present=one.client()) as stale:
            assert _refused(stale, port)  # the old server certificate is gone
    assert reloader.generation == 1


def test_a_rotated_client_ca_takes_effect_and_the_old_ca_is_refused(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    """Only the CA that signs CLIENT certificates changes. The server certificate
    stays, so both clients still trust the server: the difference is purely
    whether the server accepts them."""
    one, two = pki
    old_client, new_client = one.client("fib-sales"), two.client("fib-sales")
    with _serving(live) as (port, reloader):
        with _client(one, present=old_client) as c:
            assert _reaches_a_decision(c, port)
        with _client(one, present=new_client) as c:
            assert _refused(c, port)  # signed by a CA the server does not trust yet

        assert live.key is not None
        live.client_ca.write_bytes(two.client_ca.cert_path.read_bytes())  # CA rotation only
        assert reloader.check() is True

        with _client(one, present=new_client) as c:
            assert _reaches_a_decision(c, port)
        with _client(one, present=old_client) as c:
            assert _refused(c, port)  # the old CA no longer opens a new handshake


def test_it_works_for_a_client_that_sends_no_sni(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    """A connection by IP address sends no SNI. The swap hook must still run."""
    one, two = pki
    with _serving(live) as (port, reloader):
        _install(live, two)
        assert reloader.check() is True

        with _client(two, present=two.client()) as c:
            assert _reaches_a_decision(c, port, host="127.0.0.1")
        with _client(one, present=one.client()) as c:
            assert _refused(c, port, host="127.0.0.1")


def test_the_background_watcher_applies_a_rotation_on_its_own(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    _, two = pki
    with _serving(live, interval_s=0.1) as (port, reloader):
        _install(live, two)
        client_cert = two.client()

        deadline = time.monotonic() + 10
        with _client(two, present=client_cert) as c:
            while reloader.generation == 0:
                assert time.monotonic() < deadline, "the watcher never picked up the rotation"
                time.sleep(0.05)
            assert _reaches_a_decision(c, port)


# ── a bad rotation never empties trust ───────────────────────────────────


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda f, g: f.cert.write_text("not a certificate"), id="garbage-cert"),
        pytest.param(lambda f, g: f.key.write_text("not a key"), id="garbage-key"),  # type: ignore[union-attr]
        pytest.param(lambda f, g: f.client_ca.write_text(""), id="empty-client-ca"),
        pytest.param(lambda f, g: f.client_ca.write_text("junk"), id="garbage-client-ca"),
        pytest.param(
            lambda f, g: f.key.write_bytes(g.server_key.read_bytes()),  # type: ignore[union-attr]
            id="key-that-does-not-match-the-cert",
        ),
        pytest.param(lambda f, g: f.cert.write_bytes(b""), id="empty-cert"),
    ],
)
def test_a_bad_rotation_keeps_serving_the_previous_material(
    live: TlsFiles, pki: tuple[Generation, Generation], corrupt: Any
) -> None:
    one, two = pki
    client_cert = one.client()
    with _serving(live) as (port, reloader):
        corrupt(live, two)  # `two`'s key is used by the mismatched-key case

        assert reloader.check() is False
        assert reloader.last_error is not None
        assert reloader.generation == 0
        with _client(one, present=client_cert) as c:
            assert _reaches_a_decision(c, port)  # still serving, still trusting the old CA


def test_a_later_good_rotation_recovers_from_a_bad_one(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    _, two = pki
    with _serving(live) as (port, reloader):
        live.cert.write_text("garbage")
        assert reloader.check() is False

        _install(live, two)
        assert reloader.check() is True
        assert reloader.last_error is None

        with _client(two, present=two.client()) as c:
            assert _reaches_a_decision(c, port)


def test_a_file_that_vanishes_mid_rotation_is_ignored_not_an_error(
    live: TlsFiles, pki: tuple[Generation, Generation]
) -> None:
    one, _ = pki
    assert live.key is not None
    with _serving(live) as (port, reloader):
        live.key.unlink()  # a rotation in progress: the file will be back

        assert reloader.check() is False
        assert reloader.last_error is None  # not a failure, just not ready

        live.key.write_bytes(one.server_key.read_bytes())
        assert reloader.check() is False  # back, and identical to what is loaded
        with _client(one, present=one.client()) as c:
            assert _reaches_a_decision(c, port)


def test_unchanged_material_is_not_reloaded(live: TlsFiles) -> None:
    with _serving(live) as (_, reloader):
        assert reloader.check() is False
        assert reloader.check() is False
        assert reloader.generation == 0


def test_known_bad_material_is_not_rebuilt_on_every_poll(
    live: TlsFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    import parapetai_gateway.tls_reload as module

    with _serving(live) as (_, reloader):
        live.cert.write_text("garbage")
        calls = []
        real = module.build_context
        monkeypatch.setattr(module, "build_context", lambda f: calls.append(1) or real(f))

        for _ in range(5):
            reloader.check()

        assert len(calls) == 1  # tried once; retried only when the files change again


# ── startup fails closed; the key file is optional ───────────────────────


def test_unusable_material_at_startup_stops_the_server_instead_of_running_open(
    live: TlsFiles,
) -> None:
    live.client_ca.write_text("garbage")

    with pytest.raises(ssl.SSLError):
        build_server(_app(), host="127.0.0.1", port=0, tls=live, log_level="error")


def test_a_combined_pem_needs_no_separate_key_file(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    """Some secrets managers export a certificate's private key inside the same PEM."""
    one, _ = pki
    assert one.server_key is not None
    combined = tmp_path / "server.pem"
    combined.write_bytes(one.server_crt.read_bytes() + b"\n" + one.server_key.read_bytes())
    ca = tmp_path / "ca.crt"
    ca.write_bytes(one.client_ca.cert_path.read_bytes())
    files = TlsFiles(cert=combined, key=None, client_ca=ca, client_auth="required")

    build_context(files)  # loads without a key file

    with _serving(files) as (port, _):
        with _client(one, present=one.client()) as c:
            assert _reaches_a_decision(c, port)


def test_a_combined_pem_with_the_key_before_the_certificate_loads(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    """A secrets manager's PEM export may list the private key first, then the chain.
    OpenSSL reads either order; pin it so a Python or OpenSSL change is noticed."""
    one, _ = pki
    combined = tmp_path / "server.pem"
    combined.write_bytes(one.server_key.read_bytes() + b"\n" + one.server_crt.read_bytes())
    ca = tmp_path / "ca.crt"
    ca.write_bytes(one.client_ca.cert_path.read_bytes())

    build_context(TlsFiles(cert=combined, key=None, client_ca=ca, client_auth="required"))


def test_an_unknown_client_auth_mode_is_refused(live: TlsFiles) -> None:
    bad = TlsFiles(live.cert, live.key, live.client_ca, "maybe")

    with pytest.raises(ValueError, match="required"):
        build_context(bad)
