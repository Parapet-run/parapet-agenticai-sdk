"""parapetai_agent.control_plane -- the PEP-side half of policy bundle pull.
Verifies fetch_bundle()'s auth header and 304 handling, and that
sync_bundle_to_disk() writes files in exactly the shape PolicyEngine
expects (parapetai-agent/src/parapetai_agent/policy/engine.py: *.cedar via rglob,
entities.json as a JSON list) so a poll-then-reload cycle actually works,
not just that the HTTP call succeeds.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import respx
from httpx import Response

from parapetai_agent.control_plane import (
    BundleFetchError,
    default_pep_id,
    fetch_bundle,
    poll_once,
    run_bundle_poller,
    send_heartbeat,
    sync_bundle_to_disk,
)
from parapetai_agent.policy.engine import PolicyEngine

CONTROL_PLANE_URL = "https://control.example.internal"


@respx.mock
def test_fetch_bundle_sends_bearer_auth() -> None:
    route = respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "abc123",
                "files": {"00-base.cedar": "permit (principal, action, resource);"},
                "issued_at": 1.0,
            },
        )
    )

    bundle = fetch_bundle(CONTROL_PLANE_URL, "the-secret")

    assert bundle is not None
    assert bundle["digest"] == "abc123"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer the-secret"


@respx.mock
def test_fetch_bundle_sends_if_none_match_when_given() -> None:
    route = respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(return_value=Response(304))

    result = fetch_bundle(CONTROL_PLANE_URL, "the-secret", if_none_match="abc123")

    assert result is None
    assert route.calls.last.request.headers["If-None-Match"] == "abc123"


@respx.mock
def test_fetch_bundle_raises_on_error_status() -> None:
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(return_value=Response(401, text="nope"))

    try:
        fetch_bundle(CONTROL_PLANE_URL, "wrong-secret")
    except BundleFetchError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected BundleFetchError")


def test_sync_bundle_to_disk_writes_files_policy_engine_can_load(tmp_path: Path) -> None:
    bundle = {
        "agent_id": "pa-1",
        "digest": "abc123",
        "files": {
            "00-base.cedar": 'permit (principal, action == Action::"tool_call", resource);',
            "entities.json": "[]",
        },
    }
    policy_dir = tmp_path / "policies"

    sync_bundle_to_disk(bundle, policy_dir)

    assert (policy_dir / "00-base.cedar").read_text() == bundle["files"]["00-base.cedar"]
    assert (policy_dir / "entities.json").read_text() == "[]"
    # The real proof: PolicyEngine actually loads what was written, not just
    # that files with the right names exist.
    engine = PolicyEngine(policy_dir, policy_dir / "entities.json")
    assert engine.status["policy_files"] == 1


@respx.mock
def test_poll_once_writes_bundle_and_returns_new_digest(tmp_path: Path) -> None:
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "digest-v1",
                "files": {
                    "00-base.cedar": "permit (principal, action, resource);",
                    "entities.json": "[]",
                },
            },
        )
    )
    policy_dir = tmp_path / "policies"

    digest = poll_once(CONTROL_PLANE_URL, "the-secret", policy_dir, None)

    assert digest == "digest-v1"
    assert (policy_dir / "00-base.cedar").exists()


@respx.mock
def test_poll_once_with_engine_applies_directly_no_watcher_needed(tmp_path: Path) -> None:
    """The real fix: passing engine= means a poll alone is sufficient for a
    live update -- proves this by never calling engine.reload() or running
    any file-watcher, only poll_once(engine=...), and checking the ENGINE's
    own decisions change. This is exactly the mechanism that was missing
    from examples/maf_webapp/web_app.py: a poller that wrote
    fresh files but had nothing to apply them, leaving policy_generation
    frozen at 1 for an entire session despite multiple control-plane rule
    changes."""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text(
        'permit (principal, action == Action::"tool_call", resource);'
    )
    (policy_dir / "entities.json").write_text("[]")
    engine = PolicyEngine(policy_dir, policy_dir / "entities.json")
    assert (
        engine.evaluate(
            principal='Agent::"x"', action="tool_call", resource='Resource::"azure"', context={}
        ).allowed
        is True
    )

    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "digest-v2",
                # Narrower than what's currently loaded (tool_call permit
                # dropped) so the assertions below can tell a real reload
                # happened, not just that "something" was accepted.
                "files": {
                    "00-base.cedar": 'permit (principal, action==Action::"model_call", resource);',
                    "entities.json": "[]",
                },
            },
        )
    )

    digest = poll_once(CONTROL_PLANE_URL, "the-secret", policy_dir, None, engine=engine)

    assert digest == "digest-v2"
    assert engine.status["generation"] == 2
    assert (
        engine.evaluate(
            principal='Agent::"x"', action="tool_call", resource='Resource::"azure"', context={}
        ).allowed
        is False
    )


@respx.mock
def test_poll_once_keeps_last_digest_on_fetch_failure(tmp_path: Path) -> None:
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(return_value=Response(500))
    policy_dir = tmp_path / "policies"

    digest = poll_once(CONTROL_PLANE_URL, "the-secret", policy_dir, "previous-digest")

    assert digest == "previous-digest"
    assert not (policy_dir / "00-base.cedar").exists()


def test_default_pep_id_is_stable_within_a_process() -> None:
    assert default_pep_id() == default_pep_id()


@respx.mock
def test_send_heartbeat_posts_expected_shape() -> None:
    route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )

    send_heartbeat(
        CONTROL_PLANE_URL,
        "the-secret",
        pep_id="pep-1",
        version="0.1.0",
        policy_generation=3,
        bundle_digest="abc123",
        mode="enforce",
    )

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer the-secret"
    body = json.loads(sent.content)
    assert body == {
        "pep_id": "pep-1",
        "version": "0.1.0",
        "policy_generation": 3,
        "bundle_digest": "abc123",
        "mode": "enforce",
    }


@respx.mock
def test_send_heartbeat_failure_does_not_raise() -> None:
    respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat").mock(return_value=Response(500))

    send_heartbeat(
        CONTROL_PLANE_URL,
        "the-secret",
        pep_id="pep-1",
        version="0.1.0",
        policy_generation=1,
        bundle_digest="d",
        mode="enforce",
    )  # must not raise


@respx.mock
def test_run_bundle_poller_sends_a_heartbeat_per_cycle_when_engine_given(tmp_path: Path) -> None:
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
    heartbeat_route = respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    policy_dir = tmp_path / "policies"
    sync_bundle_to_disk(
        {
            "files": {
                "00-base.cedar": "permit (principal, action, resource);",
                "entities.json": "[]",
            }
        },
        policy_dir,
    )
    engine = PolicyEngine(policy_dir, policy_dir / "entities.json")
    stop_event = threading.Event()

    # stop_event is set from inside the heartbeat call itself (a respx
    # side effect), so the loop runs exactly one cycle deterministically
    # instead of racing a real sleep against interval_s.
    def _respond_and_stop(request: object) -> Response:
        stop_event.set()
        return Response(200, json={"status": "ok"})

    heartbeat_route.side_effect = _respond_and_stop

    run_bundle_poller(
        CONTROL_PLANE_URL,
        "the-secret",
        policy_dir,
        interval_s=0.01,
        engine=engine,
        pep_id="pep-1",
        version="0.1.0",
        mode="enforce",
        stop_event=stop_event,
    )

    assert heartbeat_route.called
