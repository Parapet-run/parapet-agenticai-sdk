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
import time
from pathlib import Path

import respx
from httpx import Response

from parapetai_agent.control_plane import (
    BundleFetchError,
    bootstrap_engine,
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
def test_poll_once_on_bundle_meta_receives_the_full_bundle_dict(tmp_path: Path) -> None:
    """on_bundle_meta is the escape hatch for bundle-level metadata beyond
    files/digest (auth-integrations.md §3/§8 Q2's vendor_scoped_resources
    is the first real user) -- unlike on_bundle, which only ever sees
    bundle["files"]."""
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "digest-v1",
                "files": {"00-base.cedar": "permit (principal, action, resource);"},
                "vendor_scoped_resources": True,
            },
        )
    )
    seen: dict[str, object] = {}

    poll_once(
        CONTROL_PLANE_URL,
        "the-secret",
        tmp_path / "policies",
        None,
        on_bundle_meta=seen.update,
    )

    assert seen["vendor_scoped_resources"] is True
    assert seen["digest"] == "digest-v1"


@respx.mock
def test_poll_once_on_bundle_meta_not_called_on_304(tmp_path: Path) -> None:
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(return_value=Response(304))
    calls: list[dict[str, object]] = []

    poll_once(
        CONTROL_PLANE_URL,
        "the-secret",
        tmp_path / "policies",
        "already-current-digest",
        on_bundle_meta=calls.append,
    )

    assert calls == []


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


@respx.mock
def test_run_bundle_poller_calls_on_bundle_meta_every_cycle(tmp_path: Path) -> None:
    # auth-integrations.md §10.3/§10.17: CollectionBudget.update_from_bundle_meta
    # (parapetai_agent.observation) is wired here exactly like this -- must
    # fire on the bundle-poll response every cycle, not just once at
    # bootstrap (unlike bootstrap_engine()'s one-shot vendor_scoped_resources
    # read), since a saturation/resume instruction needs to take effect on
    # a later poll, not only the first one.
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "observation_collection": {"saturated_buckets": ["ob-abc"]},
                "files": {
                    "00-base.cedar": "permit (principal, action, resource);",
                    "entities.json": "[]",
                },
            },
        )
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
    seen: list[dict[str, object]] = []

    def _record(bundle: dict[str, object]) -> None:
        seen.append(bundle)
        stop_event.set()

    run_bundle_poller(
        CONTROL_PLANE_URL,
        "the-secret",
        policy_dir,
        interval_s=0.01,
        engine=engine,
        stop_event=stop_event,
        on_bundle_meta=_record,
    )

    assert len(seen) == 1
    assert seen[0]["observation_collection"] == {"saturated_buckets": ["ob-abc"]}


def _mock_bootstrap_endpoints(vendor_scoped_resources: bool | None) -> None:
    """Registers the two endpoints bootstrap_engine(start_poller=False)
    actually calls: key registration and the bundle pull. `None` omits
    the field entirely -- the "older control plane, or one that never
    sends it" case Bootstrap.vendor_scoped_resources must still default
    safely for."""
    respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    files: dict[str, object] = {
        "agent_id": "pa-1",
        "digest": "d1",
        "files": {"00-base.cedar": "permit (principal, action, resource);"},
    }
    if vendor_scoped_resources is not None:
        files["vendor_scoped_resources"] = vendor_scoped_resources
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(return_value=Response(200, json=files))


@respx.mock
def test_bootstrap_engine_resolves_vendor_scoped_resources_true_from_bundle(
    tmp_path: Path,
) -> None:
    _mock_bootstrap_endpoints(vendor_scoped_resources=True)
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text("permit (principal, action, resource);")

    boot = bootstrap_engine(
        CONTROL_PLANE_URL,
        "the-secret",
        policy_dir=policy_dir,
        pep_key_path=tmp_path / "pep-key.json",
        start_poller=False,
    )

    assert boot.vendor_scoped_resources is True


@respx.mock
def test_bootstrap_engine_defaults_vendor_scoped_resources_false_when_absent(
    tmp_path: Path,
) -> None:
    """An older control plane that doesn't send this field at all -- or a
    tenant this plane hasn't turned it on for -- must resolve to unchanged
    (False) behavior, never an implicit opt-in."""
    _mock_bootstrap_endpoints(vendor_scoped_resources=None)
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text("permit (principal, action, resource);")

    boot = bootstrap_engine(
        CONTROL_PLANE_URL,
        "the-secret",
        policy_dir=policy_dir,
        pep_key_path=tmp_path / "pep-key.json",
        start_poller=False,
    )

    assert boot.vendor_scoped_resources is False


@respx.mock
def test_bootstrap_engine_on_bundle_meta_reaches_the_background_poller(tmp_path: Path) -> None:
    """Closes a real, previously-shipped gap: bootstrap_engine()'s own
    background poller thread never accepted a caller-supplied
    on_bundle_meta at all -- only the synchronous first fetch's internal
    use of this shape (reading vendor_scoped_resources back out) ever
    reached that far. A caller wiring observation.CollectionBudget.
    update_from_bundle_meta here needs a server-issued saturation/resume
    instruction to keep arriving on every later poll cycle, not just once
    at bootstrap -- auth-integrations.md §10.3/§10.17."""
    respx.post(f"{CONTROL_PLANE_URL}/api/v1/keys").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "observation_collection": {"saturated_buckets": ["ob-xyz"]},
                "files": {"00-base.cedar": "permit (principal, action, resource);"},
            },
        )
    )
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text("permit (principal, action, resource);")

    seen: list[dict[str, object]] = []

    def _record(bundle: dict[str, object]) -> None:
        seen.append(bundle)

    respx.post(f"{CONTROL_PLANE_URL}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )

    # bootstrap_engine() sends one heartbeat and one poll_once() itself,
    # synchronously, before ever starting the background thread -- so
    # `seen` already has one entry by the time it returns. Poll for the
    # thread's own first background cycle to add a second, rather than
    # racing a stop_event against a fixed sleep -- interval_s isn't
    # exposed on bootstrap_engine() at all, so the loop's first cycle
    # fires as soon as the thread schedules, with no fixed delay to wait
    # out; poll_once() runs at the very top of its loop body (before any
    # sleep) each cycle.
    boot = bootstrap_engine(
        CONTROL_PLANE_URL,
        "the-secret",
        policy_dir=policy_dir,
        pep_key_path=tmp_path / "pep-key.json",
        on_bundle_meta=_record,
    )
    deadline = time.monotonic() + 5.0
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert boot.stop_event is not None
    boot.stop_event.set()
    assert boot.thread is not None
    boot.thread.join(timeout=5.0)

    assert len(seen) == 2
    assert all(b["observation_collection"] == {"saturated_buckets": ["ob-xyz"]} for b in seen)


def test_suppressed_instrumentation_sets_and_clears_the_context_key() -> None:
    """auth-integrations.md §10.16 Phase B build note: this module's own
    control-plane HTTP calls must never be corroborated/observed as if
    they were a tool's real vendor call -- see _suppressed_instrumentation's
    own docstring. Proven at the OTel-context level directly, deliberately
    NOT by enabling real corroboration.enable_http_corroboration() in this
    (shared, in-process) pytest run -- test_corroboration.py's own module
    docstring documents that doing so, even with disable_ called
    afterward, has left this exact shared process in a state where a
    LATER test elsewhere stops recording spans; that correctness proof
    already lives in a subprocess-isolated test there. What matters here
    is only that this function sets and clears the exact key any contrib
    instrumentor checks -- a pure opentelemetry-api mechanism with no
    instrumentor involved at all."""
    from opentelemetry import context as otel_context

    from parapetai_agent.control_plane import _suppressed_instrumentation

    assert otel_context.get_value(otel_context._SUPPRESS_INSTRUMENTATION_KEY) is not True
    with _suppressed_instrumentation():
        assert otel_context.get_value(otel_context._SUPPRESS_INSTRUMENTATION_KEY) is True
    assert otel_context.get_value(otel_context._SUPPRESS_INSTRUMENTATION_KEY) is not True


@respx.mock
def test_fetch_bundle_suppresses_instrumentation_around_its_own_http_call() -> None:
    """Proves the WIRING (fetch_bundle actually uses
    _suppressed_instrumentation, not just that the helper works in
    isolation) via a spy, same reasoning as the test above for why this
    doesn't enable real corroboration."""
    import parapetai_agent.control_plane as cp_module

    respx.get(f"{CONTROL_PLANE_URL}/api/v1/bundle").mock(
        return_value=Response(
            200,
            json={
                "agent_id": "pa-1",
                "digest": "d1",
                "files": {"00-base.cedar": "permit (principal, action, resource);"},
            },
        )
    )
    import contextlib

    calls: list[str] = []
    real = cp_module._suppressed_instrumentation

    @contextlib.contextmanager
    def _spy():  # type: ignore[no-untyped-def]
        calls.append("suppressed")
        with real():
            yield

    cp_module._suppressed_instrumentation = _spy  # type: ignore[assignment]
    try:
        fetch_bundle(CONTROL_PLANE_URL, "the-secret")
    finally:
        cp_module._suppressed_instrumentation = real

    assert calls == ["suppressed"]
