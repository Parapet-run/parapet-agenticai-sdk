"""Governor.from_control_plane -- the framework-neutral embed path, governed
by control-plane-authored policy.

Before this existed, only the MAF adapter (build_middleware) could receive
policy from the control plane. Every other framework -- LangGraph, CrewAI, the
OpenAI Agents SDK, a plain loop -- was stuck on Governor.from_policy_dir(),
i.e. policy files the adopter maintains themselves, which the control plane
does not govern at all. These tests pin the behaviour that closes that gap:

  * policy actually comes from the fetched bundle (not a local file);
  * an unreachable control plane degrades to THE LAST BUNDLE ON DISK rather
    than taking the adopter's agent down with it;
  * with nothing on disk either, it fails CLOSED -- there is no policy to
    enforce, so refusing to start is the only safe answer;
  * the shared bootstrap is used, so MAF and Governor cannot drift into
    different outage semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from httpx import Response

from parapetai_agent import GovernanceDenied, Governor

CP = "https://cp.example"
PERMIT_ALL = "permit (principal, action, resource);"
# Denies the tool this suite probes, so a decision can be traced to the bundle
# it came from rather than to a default.
BUNDLE_POLICY = (
    'permit (principal, action == Action::"tool_call", resource);\n'
    '@id("no_delete")\n'
    'forbid (principal, action == Action::"tool_call", resource)\n'
    'when { context has tool_name && context.tool_name == "delete_everything" };'
)


def _mock_control_plane(policy: str = BUNDLE_POLICY, *, bundle_status: int = 200) -> None:
    """Register the three endpoints a bootstrapping PEP calls: key
    registration, bundle pull, heartbeat."""
    respx.post(f"{CP}/api/v1/keys").mock(return_value=Response(200, json={"status": "ok"}))
    respx.post(f"{CP}/api/v1/fleet/heartbeat").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    if bundle_status == 200:
        respx.get(f"{CP}/api/v1/bundle").mock(
            return_value=Response(
                200,
                json={
                    "agent_id": "pa-1",
                    "digest": "d1",
                    "files": {"00-base.cedar": policy, "entities.json": "[]"},
                },
            )
        )
    else:
        respx.get(f"{CP}/api/v1/bundle").mock(return_value=Response(bundle_status))


def _seed_local(policy_dir: Path, policy: str = PERMIT_ALL) -> Path:
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "00-base.cedar").write_text(policy)
    return policy_dir


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PARAPETAI_CONTROL_PLANE_URL",
        "PARAPETAI_AGENT_SECRET",
        "PARAPETAI_AGENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_real_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poller is a daemon thread that fetches again immediately; left
    real, it races these assertions. Patched on control_plane because that is
    where bootstrap_engine resolves the name -- patching govern's namespace
    would silently miss."""
    import parapetai_agent.control_plane as cp

    monkeypatch.setattr(cp, "run_bundle_poller", lambda *a, **k: None)


# ── policy really comes from the control plane ───────────────────────


@respx.mock
def test_policy_comes_from_the_fetched_bundle(tmp_path: Path) -> None:
    """The point of the constructor: the deny below exists ONLY in the bundle
    the control plane served, never in the local policy dir."""
    _mock_control_plane()
    local = _seed_local(tmp_path / "local")  # permit-all -- must be overridden

    gov = Governor.from_control_plane(CP, "secret", policy_dir=local)
    try:
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_everything", {})
        assert gov.authorize_tool("read_thing", {}).allowed is True
    finally:
        gov.stop_sync()


@respx.mock
def test_credentials_fall_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same env vars build_middleware reads, so one configuration serves both
    integrations."""
    _mock_control_plane()
    monkeypatch.setenv("PARAPETAI_CONTROL_PLANE_URL", CP)
    monkeypatch.setenv("PARAPETAI_AGENT_SECRET", "secret")

    gov = Governor.from_control_plane(policy_dir=_seed_local(tmp_path / "local"))
    try:
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_everything", {})
    finally:
        gov.stop_sync()


def test_missing_credentials_raise_a_directive_error(tmp_path: Path) -> None:
    """Silently falling back to local files would look like it worked while
    the control plane governed nothing."""
    with pytest.raises(RuntimeError, match="from_policy_dir"):
        Governor.from_control_plane(policy_dir=_seed_local(tmp_path / "local"))


# ── outage behaviour: the decision this constructor exists to encode ──


@respx.mock
def test_unreachable_control_plane_falls_back_to_the_last_bundle_on_disk(
    tmp_path: Path,
) -> None:
    """THE outage test. A previous run persisted a bundle; the control plane
    is now down. The agent must keep enforcing that bundle -- an outage on
    our side taking a customer's agent down is a worse failure than serving
    slightly stale policy."""
    persisted = _seed_local(tmp_path / "persisted", BUNDLE_POLICY)
    _mock_control_plane(bundle_status=503)

    gov = Governor.from_control_plane(
        CP, "secret", policy_dir=tmp_path / "unused", persist_policy_dir=persisted
    )
    try:
        # Still enforcing the persisted bundle's rule, not failing to start.
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_everything", {})
    finally:
        gov.stop_sync()


@respx.mock
def test_unreachable_control_plane_with_nothing_on_disk_fails_closed(tmp_path: Path) -> None:
    """The other half of the same decision. No bundle was ever persisted, so
    there is no policy to enforce -- starting anyway would mean running
    ungoverned while appearing governed."""
    _mock_control_plane(bundle_status=503)

    with pytest.raises(Exception) as exc:
        Governor.from_control_plane(
            CP, "secret", policy_dir=tmp_path / "empty", persist_policy_dir=tmp_path / "empty"
        )

    assert "cedar" in str(exc.value).lower() or "policy" in str(exc.value).lower()


@respx.mock
def test_successful_fetch_persists_for_the_next_cold_start(tmp_path: Path) -> None:
    """What makes the fallback above possible at all -- without the write,
    every restart during an outage would fail closed."""
    persisted = tmp_path / "persisted"
    _mock_control_plane()

    gov = Governor.from_control_plane(
        CP, "secret", policy_dir=tmp_path / "unused", persist_policy_dir=persisted
    )
    try:
        assert (persisted / "00-base.cedar").read_text() == BUNDLE_POLICY
    finally:
        gov.stop_sync()


@respx.mock
def test_without_persist_dir_nothing_is_written_to_disk(tmp_path: Path) -> None:
    """Default path: the bundle is applied in memory only. An adopter who
    never asked for a disk target should not find policy files appearing in
    their working tree."""
    local = _seed_local(tmp_path / "local")
    _mock_control_plane()

    gov = Governor.from_control_plane(CP, "secret", policy_dir=local)
    try:
        # The in-memory bundle is enforced...
        with pytest.raises(GovernanceDenied):
            gov.authorize_tool("delete_everything", {})
        # ...but the local dir still holds only what the test seeded.
        assert sorted(p.name for p in local.iterdir()) == ["00-base.cedar"]
        assert local.joinpath("00-base.cedar").read_text() == PERMIT_ALL
    finally:
        gov.stop_sync()


# ── lifecycle ────────────────────────────────────────────────────────


@respx.mock
def test_stop_sync_is_idempotent(tmp_path: Path) -> None:
    _mock_control_plane()
    gov = Governor.from_control_plane(CP, "secret", policy_dir=_seed_local(tmp_path / "local"))

    gov.stop_sync()
    gov.stop_sync()  # must not raise


def test_stop_sync_is_a_no_op_on_a_local_governor(tmp_path: Path) -> None:
    """A caller should be able to call stop_sync() without knowing which
    constructor produced the Governor."""
    Governor.from_policy_dir(_seed_local(tmp_path / "local")).stop_sync()


# ── one bootstrap, not two ───────────────────────────────────────────


def test_shares_the_bootstrap_with_the_maf_adapter() -> None:
    """Both entry points must resolve to the SAME implementation. Two copies
    would mean two sets of outage semantics, so "the agent acts as
    configured" could differ by which integration a customer picked -- the
    duplication this refactor removed."""
    import parapetai_agent.control_plane as cp
    import parapetai_agent.maf as maf

    assert maf.bootstrap_engine is cp.bootstrap_engine


def test_heartbeat_version_reports_this_sdk_not_the_gateway() -> None:
    """Regression guard on a real copy-paste bug: maf.py carried its own
    _installed_version() that asked importlib.metadata for
    "parapetai-gateway" -- correct in the gateway's identical helper, wrong
    here. The gateway package is normally absent from an embedded SDK, so
    every SDK PEP reported "0.0.0-dev" in the heartbeat, and on a host that
    happened to have the gateway installed it reported the GATEWAY's version
    for an SDK PEP.

    That field is how the fleet table evidences "this agent is acting as
    configured", so a wrong or unknown value undermines the claim it exists
    to support.
    """
    from importlib.metadata import version as pkg_version

    from parapetai_agent.control_plane import sdk_version

    assert sdk_version() == pkg_version("parapetai-agent")
    assert sdk_version() != "0.0.0-dev"
