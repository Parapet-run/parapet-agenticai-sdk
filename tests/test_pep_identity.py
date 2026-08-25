"""parapetai_agent.pep_identity -- a PEP's persistent Ed25519 keypair, and the
signing this repo's control-plane requests use it for
(parapetai_agent.control_plane).

What matters here that a unit test of pep_identity.py alone can't prove:
that a real Ed25519PublicKey.verify() call actually accepts what
sign_headers() produces (not just that it "looks like" a signature).
There used to be a second thing proven here -- that this module's
signing_payload matched control-plane/src/parapetai_control/keys.py's own copy
byte-for-byte -- but both sides now import the same
parapetai_agent.signing.signing_payload, so there's nothing left to prove;
drift is impossible by construction, not merely untested.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import Response

from parapetai_agent import pep_identity
from parapetai_agent.control_plane import (
    ensure_pep_identity,
    fetch_bundle,
    register_public_key,
    run_bundle_poller,
    send_heartbeat,
)
from parapetai_agent.signing import signing_payload

CONTROL_PLANE_URL = "https://control.example.internal"


def test_keypair_persists_across_loads(tmp_path: Path) -> None:
    key_path = tmp_path / "pep.key"
    first = pep_identity.load_or_create_keypair(key_path)
    second = pep_identity.load_or_create_keypair(key_path)
    assert pep_identity.public_key_b64(first) == pep_identity.public_key_b64(second)


def test_keypair_file_is_private(tmp_path: Path) -> None:
    key_path = tmp_path / "pep.key"
    pep_identity.load_or_create_keypair(key_path)
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_different_paths_get_different_keys(tmp_path: Path) -> None:
    a = pep_identity.load_or_create_keypair(tmp_path / "a.key")
    b = pep_identity.load_or_create_keypair(tmp_path / "b.key")
    assert pep_identity.public_key_b64(a) != pep_identity.public_key_b64(b)


def test_sign_headers_produces_a_real_verifiable_signature() -> None:
    private_key = Ed25519PrivateKey.generate()
    headers = pep_identity.sign_headers(private_key, "GET", "/api/v1/bundle", b"")
    assert "X-Parapetai-Signature" in headers
    assert "X-Parapetai-Signed-At" in headers
    import base64

    payload = signing_payload("GET", "/api/v1/bundle", headers["X-Parapetai-Signed-At"], b"")
    signature = base64.b64decode(headers["X-Parapetai-Signature"])
    private_key.public_key().verify(signature, payload)  # raises if invalid


def test_sign_headers_rejects_under_a_different_payload() -> None:
    import base64

    private_key = Ed25519PrivateKey.generate()
    headers = pep_identity.sign_headers(private_key, "GET", "/api/v1/bundle", b"")
    signature = base64.b64decode(headers["X-Parapetai-Signature"])
    wrong_payload = signing_payload("POST", "/api/v1/bundle", headers["X-Parapetai-Signed-At"], b"")
    try:
        private_key.public_key().verify(signature, wrong_payload)
        raise AssertionError("expected InvalidSignature")
    except InvalidSignature:
        pass


@respx.mock
def test_fetch_bundle_signs_when_a_private_key_is_given() -> None:
    route = respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={"agent_id": "pa-1", "digest": "d1", "files": {}, "issued_at": 1.0},
        )
    )
    private_key = Ed25519PrivateKey.generate()

    fetch_bundle(CONTROL_PLANE_URL, "secret", private_key=private_key)

    sent = route.calls.last.request
    assert "X-Parapetai-Signature" in sent.headers
    assert "X-Parapetai-Signed-At" in sent.headers
    import base64

    payload = signing_payload("GET", "/api/v1/bundle", sent.headers["X-Parapetai-Signed-At"], b"")
    signature = base64.b64decode(sent.headers["X-Parapetai-Signature"])
    private_key.public_key().verify(signature, payload)


@respx.mock
def test_fetch_bundle_does_not_sign_without_a_private_key() -> None:
    """The default, unsigned path -- every existing caller/test that
    doesn't know pep_identity exists must be completely unaffected."""
    route = respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(200, json={"agent_id": "pa-1", "digest": "d1", "files": {}})
    )
    fetch_bundle(CONTROL_PLANE_URL, "secret")
    sent = route.calls.last.request
    assert "X-Parapetai-Signature" not in sent.headers


@respx.mock
def test_send_heartbeat_signs_the_exact_body_bytes_sent() -> None:
    route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    private_key = Ed25519PrivateKey.generate()

    send_heartbeat(
        CONTROL_PLANE_URL,
        "secret",
        pep_id="pep-1",
        version="1.0",
        policy_generation=1,
        bundle_digest="d1",
        mode="enforce",
        private_key=private_key,
    )

    sent = route.calls.last.request
    import base64

    payload = signing_payload(
        "POST", "/api/v1/fleet/heartbeat", sent.headers["X-Parapetai-Signed-At"], sent.content
    )
    signature = base64.b64decode(sent.headers["X-Parapetai-Signature"])
    private_key.public_key().verify(signature, payload)  # raises if the body doesn't match


@respx.mock
def test_register_public_key_posts_the_correct_shape() -> None:
    route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"agent_id": "pa-1", "status": "registered"})
    )
    private_key = Ed25519PrivateKey.generate()

    ok = register_public_key(CONTROL_PLANE_URL, "secret", private_key)

    assert ok is True
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"
    body = json.loads(sent.content)
    assert body == {"public_key": pep_identity.public_key_b64(private_key)}


@respx.mock
def test_register_public_key_failure_does_not_raise() -> None:
    respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(return_value=Response(500))
    private_key = Ed25519PrivateKey.generate()
    assert register_public_key(CONTROL_PLANE_URL, "secret", private_key) is False


@respx.mock
def test_ensure_pep_identity_loads_and_registers(tmp_path: Path) -> None:
    route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "registered"})
    )
    key_path = tmp_path / "pep.key"

    private_key = ensure_pep_identity(CONTROL_PLANE_URL, "secret", key_path)

    assert key_path.exists()
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["public_key"] == pep_identity.public_key_b64(private_key)


def test_generate_ephemeral_keypair_never_touches_disk(tmp_path: Path) -> None:
    """No key_path argument at all -- if this touched disk anywhere, it
    would have to guess a path, which it structurally can't."""
    key = pep_identity.generate_ephemeral_keypair()
    assert isinstance(key, Ed25519PrivateKey)
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere


def test_generate_ephemeral_keypair_is_different_every_call() -> None:
    first = pep_identity.generate_ephemeral_keypair()
    second = pep_identity.generate_ephemeral_keypair()
    assert first.private_bytes_raw() != second.private_bytes_raw()


@respx.mock
def test_ensure_pep_identity_with_persist_false_never_touches_disk(tmp_path: Path) -> None:
    """persist=False -- registers a real (ephemeral) key without ever
    reading/writing key_path, even when one is given (ignored, not used
    as a hint)."""
    route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "registered"})
    )
    key_path = tmp_path / "pep.key"

    private_key = ensure_pep_identity(CONTROL_PLANE_URL, "secret", key_path, persist=False)

    assert not key_path.exists()
    assert list(tmp_path.iterdir()) == []
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["public_key"] == pep_identity.public_key_b64(private_key)


@respx.mock
def test_ensure_pep_identity_with_persist_false_and_no_key_path_still_works(
    tmp_path: Path,
) -> None:
    """The realistic serverless call shape -- no key_path at all, not
    even one that happens to be ignored."""
    respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "registered"})
    )
    private_key = ensure_pep_identity(CONTROL_PLANE_URL, "secret", persist=False)
    assert isinstance(private_key, Ed25519PrivateKey)


@respx.mock
def test_run_bundle_poller_signs_every_cycle_when_private_key_given(tmp_path: Path) -> None:
    bundle_route = respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "files": {
                    "00-base.cedar": "permit (principal, action, resource);",
                    "entities.json": "[]",
                },
            },
        )
    )
    private_key = Ed25519PrivateKey.generate()
    policy_dir = tmp_path / "policies"
    stop_event = threading.Event()

    def _stop(request: object) -> Response:
        stop_event.set()
        return Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "files": {"00-base.cedar": "permit (principal, action, resource);"},
            },
        )

    bundle_route.side_effect = _stop

    run_bundle_poller(
        CONTROL_PLANE_URL,
        "secret",
        policy_dir,
        interval_s=0.01,
        private_key=private_key,
        stop_event=stop_event,
    )

    sent = bundle_route.calls.last.request
    assert "X-Parapetai-Signature" in sent.headers


@respx.mock
def test_run_bundle_poller_rotates_on_control_plane_request(tmp_path: Path) -> None:
    """The end of the loop this whole feature exists for: an operator's
    "trigger rotation" (parapetai_control/agent_auth.py) surfaces as rotate_key: true
    on a heartbeat response -- proves run_bundle_poller reacts by writing
    a genuinely NEW keypair to key_path and registering it, not just
    logging the signal."""
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "files": {
                    "00-base.cedar": "permit (principal, action, resource);",
                    "entities.json": "[]",
                },
            },
        )
    )
    keys_route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "registered"})
    )
    heartbeat_route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat")

    key_path = tmp_path / "pep.key"
    original_key = pep_identity.load_or_create_keypair(key_path)
    original_public = pep_identity.public_key_b64(original_key)

    stop_event = threading.Event()
    call_count = {"n": 0}

    def _heartbeat_responder(request: object) -> Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return Response(200, json={"status": "ok", "rotate_key": True})
        stop_event.set()
        return Response(200, json={"status": "ok", "rotate_key": False})

    heartbeat_route.side_effect = _heartbeat_responder

    from parapetai_agent.policy.engine import PolicyEngine

    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text("permit (principal, action, resource);")
    (policy_dir / "entities.json").write_text("[]")
    engine = PolicyEngine(policy_dir, policy_dir / "entities.json")

    run_bundle_poller(
        CONTROL_PLANE_URL,
        "secret",
        policy_dir,
        interval_s=0.01,
        engine=engine,
        pep_id="pep-1",
        private_key=original_key,
        key_path=key_path,
        stop_event=stop_event,
    )

    assert call_count["n"] == 2
    assert keys_route.call_count == 1  # only the post-rotation registration

    rotated_key = pep_identity.load_or_create_keypair(key_path)
    assert pep_identity.public_key_b64(rotated_key) != original_public

    import json as jsonlib

    registered_body = jsonlib.loads(keys_route.calls.last.request.content)
    assert registered_body["public_key"] == pep_identity.public_key_b64(rotated_key)
