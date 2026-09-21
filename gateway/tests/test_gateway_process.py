"""The real gateway process, end to end, laid out the way it runs in a pod.

Everything else in the suite drives the app or the server builder in-process.
This runs `python -m parapetai_gateway.server.main` -- the actual entrypoint --
so it exercises the wiring in main.py (health listener, admin switch, mTLS,
reload thread) and the file layout Kubernetes produces.

The certificate directory imitates what a secrets-store CSI driver and
Kubernetes Secret volumes produce: the visible files are
symlinks through a `..data` symlink, and a rotation writes a new generation
directory and then atomically repoints `..data`. A watcher keyed on file mtime
misses that; one keyed on content does not. The certificate is a combined PEM
(chain + private key in one file, no separate key), which is how some secrets
managers export it.

What this cannot cover is the hosting environment itself (the secrets manager,
the CSI driver, the load balancer). That is each site's own deployment test.
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from .identity_support import Ca, issue_client_cert, issue_server_cert, make_ca

REPO_POLICIES = Path(__file__).resolve().parents[2] / "policies"
_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "execute_shell"},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Generation:
    server_ca: Ca
    client_ca: Ca
    combined_pem: bytes  # server cert chain + private key, as some secrets managers export it
    directory: Path

    def client(self, cn: str = "fib-sales") -> tuple[Path, Path]:
        return issue_client_cert(self.client_ca, self.directory, cn, name=f"c-{cn}")


def _generation(root: Path, name: str) -> Generation:
    directory = root / name
    directory.mkdir()
    server_ca = make_ca(directory, f"{name}-server-ca")
    client_ca = make_ca(directory, f"{name}-client-ca")
    crt, key = issue_server_cert(server_ca, directory)
    return Generation(server_ca, client_ca, crt.read_bytes() + b"\n" + key.read_bytes(), directory)


@pytest.fixture
def pki(tmp_path: Path) -> tuple[Generation, Generation]:
    return _generation(tmp_path, "one"), _generation(tmp_path, "two")


class CsiMount:
    """A directory shaped like a CSI / Secret volume mount."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self._n = 0

    def publish(self, gen: Generation) -> None:
        """Write a new generation and atomically repoint `..data` at it."""
        self._n += 1
        name = f"..gen{self._n}"
        (self.root / name).mkdir()
        (self.root / name / "server.pem").write_bytes(gen.combined_pem)
        (self.root / name / "client-ca.crt").write_bytes(gen.client_ca.cert_path.read_bytes())
        tmp = self.root / "..data_tmp"
        os.symlink(name, tmp)
        os.replace(tmp, self.root / "..data")  # the atomic swap kubelet/CSI perform
        for visible in ("server.pem", "client-ca.crt"):
            link = self.root / visible
            if not link.is_symlink():
                os.symlink(f"..data/{visible}", link)

    @property
    def cert(self) -> Path:
        return self.root / "server.pem"

    @property
    def client_ca(self) -> Path:
        return self.root / "client-ca.crt"


@dataclass
class Running:
    process: subprocess.Popen[bytes]
    port: int
    health_port: int
    log: Path

    def output(self) -> str:
        return self.log.read_text(errors="replace")


@contextmanager
def _gateway(
    tmp_path: Path, mount: CsiMount, *, extra_env: dict[str, str] | None = None
) -> Iterator[Running]:
    port, health_port = _free_port(), _free_port()
    bindings = tmp_path / "bindings.json"
    bindings.write_text('[{"kind": "mtls", "cn": "fib-sales", "agent_id": "agent-mtls"}]')
    env = {k: v for k, v in os.environ.items() if not k.startswith("PARAPETAI_")}
    env.update(
        {
            "PARAPETAI_HOST": "127.0.0.1",
            "PARAPETAI_PORT": str(port),
            "PARAPETAI_HEALTH_PORT": str(health_port),
            "PARAPETAI_POLICY_DIR": str(REPO_POLICIES),
            "PARAPETAI_ENTITIES_PATH": str(REPO_POLICIES / "entities.json"),
            "PARAPETAI_TLS_CERT": str(mount.cert),  # combined PEM: no PARAPETAI_TLS_KEY
            "PARAPETAI_TLS_CLIENT_CA": str(mount.client_ca),
            "PARAPETAI_TLS_CLIENT_AUTH": "required",
            "PARAPETAI_TLS_RELOAD_INTERVAL_S": "0.2",
            "PARAPETAI_IDENTITY_BINDINGS": str(bindings),
            "PARAPETAI_REQUIRE_VERIFIED_IDENTITY": "true",
            "PARAPETAI_ADMIN_ROUTES": "false",
            "PARAPETAI_MODE": "enforce",
            **(extra_env or {}),
        }
    )
    log = tmp_path / "gateway.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(  # noqa: S603 -- our own entrypoint, test-controlled env
            [sys.executable, "-m", "parapetai_gateway.server.main"],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    running = Running(process, port, health_port, log)
    try:
        yield running
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _wait_ready(gw: Running, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gw.process.poll() is not None:
            pytest.fail(f"gateway exited early ({gw.process.returncode}):\n{gw.output()[-2000:]}")
        try:
            if httpx.get(
                f"http://127.0.0.1:{gw.health_port}/__parapetai/ready", timeout=1
            ).is_success:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    pytest.fail(f"gateway never became ready:\n{gw.output()[-2000:]}")


def _client(gen: Generation, present: tuple[Path, Path] | None) -> httpx.Client:
    context = ssl.create_default_context(cafile=str(gen.server_ca.cert_path))
    if present:
        context.load_cert_chain(str(present[0]), str(present[1]))
    return httpx.Client(verify=context, timeout=5)


def _decides(client: httpx.Client, gw: Running) -> bool:
    resp = client.post(f"https://localhost:{gw.port}/mcp", json=_BODY)
    return "x-parapetai-decision" in resp.headers


def _decides_eventually(client: httpx.Client, gw: Running) -> bool:
    """`_decides`, but a failed connection means "not yet": until the reload
    lands the client cannot even verify the old server certificate."""
    try:
        return _decides(client, gw)
    except httpx.TransportError:
        return False


def _refused(client: httpx.Client, gw: Running) -> bool:
    try:
        client.post(f"https://localhost:{gw.port}/mcp", json=_BODY)
    except httpx.TransportError:
        return True
    return False


def _wait_until(predicate, timeout: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ── the pod's shape ──────────────────────────────────────────────────────


def test_the_process_terminates_mtls_and_keeps_its_probe_port_plain_and_minimal(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    one, _ = pki
    mount = CsiMount(tmp_path / "tls")
    mount.publish(one)
    with _gateway(tmp_path, mount) as gw:
        _wait_ready(gw)

        # The probe port: plain HTTP, no certificate, up/down and nothing else.
        assert httpx.get(f"http://127.0.0.1:{gw.health_port}/__parapetai/health").json() == {
            "status": "ok"
        }
        ready = httpx.get(f"http://127.0.0.1:{gw.health_port}/__parapetai/ready").json()
        assert ready == {"status": "ready"}
        assert (
            httpx.get(f"http://127.0.0.1:{gw.health_port}/__parapetai/policies").status_code == 404
        )
        assert httpx.get(f"http://127.0.0.1:{gw.health_port}/mcp").status_code in (404, 405)

        # The agent port: TLS, and a client certificate is mandatory.
        with _client(one, present=None) as anonymous:
            assert _refused(anonymous, gw)
        with _client(one, present=one.client()) as agent:
            assert _decides(agent, gw)  # reached a Cedar decision: identity verified
            # Admin routes are off on the agent-facing port.
            for path in ("/__parapetai/policies", "/__parapetai/observations"):
                assert agent.get(f"https://localhost:{gw.port}{path}").status_code == 404


def test_a_csi_style_symlink_swap_is_picked_up_without_restarting_the_process(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    one, two = pki
    mount = CsiMount(tmp_path / "tls")
    mount.publish(one)
    with _gateway(tmp_path, mount) as gw:
        _wait_ready(gw)
        pid = gw.process.pid
        with _client(one, present=one.client()) as c:
            assert _decides(c, gw)

        mount.publish(two)  # new server cert AND new client CA, atomically

        new_client = two.client()
        with _client(two, present=new_client) as c:
            assert _wait_until(lambda: _decides_eventually(c, gw)), gw.output()[-2000:]
        with _client(one, present=one.client("fib-sales")) as c:
            assert _refused(c, gw)  # the previous generation no longer opens a handshake
        assert gw.process.pid == pid and gw.process.poll() is None  # no restart
        assert httpx.get(f"http://127.0.0.1:{gw.health_port}/__parapetai/health").is_success
        assert "tls_reloaded" in gw.output()


def test_a_corrupt_rotation_is_logged_and_ignored_and_the_previous_material_keeps_serving(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    one, two = pki
    mount = CsiMount(tmp_path / "tls")
    mount.publish(one)
    broken = Generation(
        two.server_ca, two.client_ca, b"-----BEGIN CERTIFICATE-----\nnope", two.directory
    )
    with _gateway(tmp_path, mount) as gw:
        _wait_ready(gw)

        mount.publish(broken)

        assert _wait_until(lambda: "tls_reload_failed_keeping_previous" in gw.output())
        with _client(one, present=one.client()) as c:
            assert _decides(c, gw)  # still trusting and presenting generation one
        assert gw.process.poll() is None

        mount.publish(two)  # and a good rotation afterwards recovers
        with _client(two, present=two.client()) as c:
            assert _wait_until(lambda: _decides_eventually(c, gw))


def test_the_process_refuses_to_start_when_the_tls_material_is_unusable(
    tmp_path: Path, pki: tuple[Generation, Generation]
) -> None:
    """Fail closed at boot: a gateway that came up without client verification
    would look exactly like one that worked."""
    one, _ = pki
    mount = CsiMount(tmp_path / "tls")
    mount.publish(one)
    mount.client_ca.resolve().write_text("garbage, not a CA")
    with _gateway(tmp_path, mount) as gw:
        assert _wait_until(lambda: gw.process.poll() is not None, timeout=30)
        assert gw.process.returncode != 0
        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{gw.health_port}/__parapetai/health", timeout=1)
