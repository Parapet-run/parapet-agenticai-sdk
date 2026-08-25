"""PEP -> control plane: pull policy bundles, authenticated with the bearer
secret issued at provisioning (parapetai_control.bundles.provision_agent on the control
plane side). Poll/pull, PEP-initiated -- matches CLAUDE.md's architecture
diagram ("policy bundle (pull)").

poll_once() does two independent things with a freshly fetched bundle:
writes it to policy_dir (sync_bundle_to_disk -- persistence, so a restart
while the control plane is briefly unreachable still has the last-known-good
bundle to load), and, when an `engine` is given, applies it directly to that
engine's memory (PolicyEngine.load_from_bundle -- the live update). These
used to be coupled through the filesystem: write to disk, then depend on a
SEPARATE watcher thread (parapetai_gateway.server.main._watch, watchfiles) noticing the
write and calling engine.reload(). That indirection was the actual root
cause of a real bug: a caller (examples/maf_webapp/web_app.py)
that started a poller but never started the matching watcher kept serving
its first-ever policy_generation forever, silently, while still reporting
"bundle_synced" -- the write worked, nothing ever read it back. Passing
`engine` here removes the second thread as a requirement for correctness;
parapetai_gateway.server.main keeps its own watcher too, for the independent case of a
hand-edited local file the poller never touched.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from parapetai_agent import pep_identity

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from parapetai_agent.policy.engine import PolicyEngine

log = structlog.get_logger(__name__)


def default_pep_id() -> str:
    """Stable for the lifetime of one process -- generated once at import,
    not per call, so repeated build_middleware() calls in the same process
    (e.g. one per scenario in examples/maf_webapp/run_example.py)
    report as the same fleet member instead of minting a new one each time.
    PARAPETAI_PEP_ID overrides this when a caller wants a stable identity across
    process restarts too (matches PARAPETAI_AGENT_ID's own env-var convention)."""
    return os.environ.get("PARAPETAI_PEP_ID") or f"pep-{socket.gethostname()}-{os.getpid()}"


class BundleFetchError(RuntimeError):
    """A bundle poll failed -- network error or non-2xx/304 response."""


def register_public_key(
    control_plane_url: str, agent_secret: str, private_key: Ed25519PrivateKey
) -> bool:
    """POSTs this PEP's public key to /api/v1/keys, authenticated with the
    bearer secret (a public key alone asserts nothing -- see
    parapetai_control/keys.py's module docstring for why registration itself still
    needs the secret even though subsequent requests won't). Idempotent:
    re-registering the SAME key the process already holds is harmless --
    it just demotes "current" to "previous" as the same value, which
    still verifies fine on either side (parapetai_control.keys.set_current_key's
    docstring covers this). Best-effort, like fetch_bundle/send_heartbeat:
    logs and returns False on failure rather than raising, so a control
    plane hiccup during startup doesn't crash the PEP over a security
    UPGRADE it can still operate without."""
    try:
        response = httpx.post(
            f"{control_plane_url.rstrip('/')}/api/v1/keys",
            headers={"Authorization": f"Bearer {agent_secret}"},
            json={"public_key": pep_identity.public_key_b64(private_key)},
            timeout=10.0,
        )
        if response.status_code != 200:
            log.error(
                "key_registration_failed",
                status_code=response.status_code,
                body=response.text[:200],
            )
            return False
        return True
    except httpx.HTTPError as exc:
        log.error("key_registration_failed", error=str(exc))
        return False


def ensure_pep_identity(
    control_plane_url: str,
    agent_secret: str,
    key_path: str | Path | None = None,
    *,
    persist: bool = True,
) -> Ed25519PrivateKey:
    """Loads or creates this PEP's persistent Ed25519 keypair
    (parapetai_agent.pep_identity) and registers its public key with the control
    plane -- the one synchronous setup step both parapetai_gateway.server.main and
    parapetai_agent.maf.build_middleware's control-plane paths need before
    any signed request, at the same point they already do their first
    synchronous poll_once(). Registration failing here is not fatal (see
    register_public_key) -- the returned key is still used to sign
    requests either way; if registration never actually landed, the
    control plane simply never enforces signatures for this agent yet
    (parapetai_control/keys.py's gradual-enforcement design), so signing anyway costs
    nothing and self-heals the next time this runs successfully.

    persist=False (default True) skips disk entirely -- a fresh,
    never-written keypair every call (parapetai_agent.pep_identity.
    generate_ephemeral_keypair(), see its own docstring for the real
    tradeoff: identity stability across restarts, not correctness) -- for
    a process with no writable filesystem at all. key_path is ignored
    when persist=False."""
    private_key = (
        pep_identity.generate_ephemeral_keypair()
        if not persist
        else pep_identity.load_or_create_keypair(Path(key_path) if key_path else None)
    )
    register_public_key(control_plane_url, agent_secret, private_key)
    return private_key


def fetch_bundle(
    control_plane_url: str,
    agent_secret: str,
    *,
    if_none_match: str | None = None,
    private_key: Ed25519PrivateKey | None = None,
) -> dict[str, Any] | None:
    """Returns the bundle response dict, or None if the control plane
    reports 304 Not Modified (bundle unchanged since if_none_match).
    Raises BundleFetchError on any other failure -- the caller decides
    whether to keep serving the last-written-to-disk bundle.

    private_key, when given, signs the request (X-Parapetai-Signature/
    X-Parapetai-Signed-At headers) -- omitted entirely when None, which is also
    what every existing caller/test that doesn't know this exists still
    gets: unsigned, exactly today's behavior. The control plane only
    requires these headers for an agent that has actually registered a
    key (parapetai_control/keys.py) -- sending them unconditionally when a key IS
    available is harmless either way."""
    path = "/api/v1/bundle"
    headers = {"Authorization": f"Bearer {agent_secret}"}
    if if_none_match:
        headers["If-None-Match"] = if_none_match
    if private_key is not None:
        headers.update(pep_identity.sign_headers(private_key, "GET", path))
    try:
        response = httpx.get(
            f"{control_plane_url.rstrip('/')}{path}", headers=headers, timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise BundleFetchError(str(exc)) from exc
    if response.status_code == 304:
        return None
    if response.status_code != 200:
        raise BundleFetchError(
            f"control plane returned {response.status_code}: {response.text[:200]}"
        )
    result: dict[str, Any] = response.json()
    return result


#: Bundle artifacts this module intentionally never writes to disk because
#: nothing ever reads them back FROM disk -- they're consumed straight from
#: the in-memory `bundle["files"]` dict via poll_once()'s on_bundle hook
#: instead (see that param's own docstring). A literal here, not an import
#: of parapetai_agent.content_checks.BUNDLE_FILENAME: this module is shared by
#: parapetai_gateway.server too (CLAUDE.md -- parapetai-agent "owns nothing specific to
#: either"), so it must not depend on a parapetai-agent-only module just to know
#: this one filename. Distinct from a genuinely unrecognised filename
#: (a provider bug, or a future artifact this SDK version doesn't know
#: about yet) -- those still warn loudly below.
_KNOWN_NON_DISK_ARTIFACTS = frozenset({"content_checks.json"})


def sync_bundle_to_disk(bundle: dict[str, Any], policy_dir: str | Path) -> None:
    """Writes a fetched bundle's files into policy_dir -- .cedar files as-is,
    entities.json as-is. Overwrites whatever was there; the control plane is
    the source of truth once an agent_id/agent_secret is configured."""
    target = Path(policy_dir)
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = bundle["files"]
    for filename, content in files.items():
        if filename == "entities.json":
            (target / filename).write_text(content)
        elif filename.endswith(".cedar"):
            (target / filename).write_text(content)
        elif filename not in _KNOWN_NON_DISK_ARTIFACTS:
            log.warning("bundle_file_skipped_unrecognised_extension", filename=filename)


def poll_once(
    control_plane_url: str,
    agent_secret: str,
    policy_dir: str | Path | None,
    last_digest: str | None,
    *,
    engine: PolicyEngine | None = None,
    private_key: Ed25519PrivateKey | None = None,
    persist_to_disk: bool = True,
    on_bundle: Callable[[dict[str, str]], None] | None = None,
) -> str | None:
    """One fetch-and-sync cycle. Returns the new digest (unchanged from
    last_digest if the bundle was 304/unmodified), or last_digest again on a
    fetch failure -- logged, never raised, so a poller loop keeps running
    through transient control-plane outages.

    Writes to disk (persistence) by default -- `persist_to_disk=False`
    skips this entirely (and `policy_dir` becomes irrelevant/may be
    None), for a caller with nowhere durable to write (a serverless/k8s
    process with no writable volume -- see parapetai_agent.maf.build_middleware's
    `persist_policy_dir` param, the only current caller that ever passes
    False; parapetai_gateway.server.main's own HTTP gateway path always wants disk
    persistence and never sets this). When `engine` is given, the bundle
    is ALSO applied directly to it via load_from_bundle() -- the live
    update, independent of (and, when persist_to_disk=False, the ONLY
    way the fetched bundle is applied at all). A failed in-memory apply
    (malformed bundle) is logged, not raised: same fail-closed contract
    as engine.reload() -- the engine keeps serving its last-good policy,
    this loop keeps polling.

    on_bundle, when given, is called with the raw `bundle["files"]` dict
    on every successful (non-304) fetch, independent of `engine` -- a
    generic escape hatch for a caller that needs to consume a bundle
    artifact PolicyEngine itself has no concept of (e.g. parapetai_agent's
    tier-2 content-check scanner config, content_checks.json -- see
    parapetai_agent/content_checks.py). Deliberately untyped beyond
    `dict[str, str]` here: this module is shared by both parapetai_gateway.server and
    parapetai_agent (CLAUDE.md: parapetai-agent "owns nothing specific to
    either"), so it must not import a parapetai-agent-only type just to type this
    callback more narrowly. A raising callback propagates -- same
    "caller's bug, don't swallow it" posture as everything else in this
    fetch-then-apply chain that ISN'T explicitly a best-effort network
    call.

    private_key, when given, is passed straight through to fetch_bundle()
    to sign the request -- see that function's own docstring."""
    try:
        bundle = fetch_bundle(
            control_plane_url, agent_secret, if_none_match=last_digest, private_key=private_key
        )
    except BundleFetchError as exc:
        log.error("bundle_poll_failed", error=str(exc))
        return last_digest
    if bundle is None:
        return last_digest
    if persist_to_disk:
        assert policy_dir is not None  # caller contract: required unless persist_to_disk=False
        sync_bundle_to_disk(bundle, policy_dir)
    if engine is not None:
        result = engine.load_from_bundle(bundle["files"])
        if result["status"] == "failed":
            log.error("policy_apply_rejected", **result)
    if on_bundle is not None:
        on_bundle(bundle["files"])
    log.info("bundle_synced", digest=bundle["digest"], files=list(bundle["files"]))
    return str(bundle["digest"])


def send_heartbeat(
    control_plane_url: str,
    agent_secret: str,
    *,
    pep_id: str,
    version: str,
    policy_generation: int,
    bundle_digest: str,
    mode: str,
    private_key: Ed25519PrivateKey | None = None,
) -> dict[str, Any] | None:
    """POSTs to /api/v1/fleet/heartbeat -- what makes this PEP show up on
    the control plane's dashboard "Fleet" table at all; that table already
    renders whatever it's given (parapetai_control/templates/dashboard.html), it was
    only ever missing a sender. Best-effort like poll_once: logs and
    returns None on failure rather than raising, so a control-plane hiccup
    doesn't take down whatever loop is calling this.

    private_key, when given, signs the request body -- serialized to JSON
    bytes ONCE, here, and sent via `content=` rather than httpx's own
    `json=` kwarg specifically so the bytes that get signed are
    byte-for-byte identical to the bytes that get sent; relying on
    httpx's internal json-encoding to happen to match a separately-signed
    payload would be signing something that isn't provably what's on the
    wire.

    Returns the parsed response body on success -- run_bundle_poller reads
    `rotate_key` off it to act on an operator's control-plane-initiated
    rotation request (parapetai_control/agent_auth.py); callers that don't care are
    free to ignore the return value, unchanged from before this existed."""
    path = "/api/v1/fleet/heartbeat"
    body = {
        "pep_id": pep_id,
        "version": version,
        "policy_generation": policy_generation,
        "bundle_digest": bundle_digest,
        "mode": mode,
    }
    body_bytes = json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {agent_secret}", "Content-Type": "application/json"}
    if private_key is not None:
        headers.update(pep_identity.sign_headers(private_key, "POST", path, body_bytes))
    try:
        response = httpx.post(
            f"{control_plane_url.rstrip('/')}{path}",
            headers=headers,
            content=body_bytes,
            timeout=10.0,
        )
        if response.status_code != 200:
            log.error(
                "heartbeat_failed", status_code=response.status_code, body=response.text[:200]
            )
            return None
        result: dict[str, Any] = response.json()
        return result
    except httpx.HTTPError as exc:
        log.error("heartbeat_failed", error=str(exc))
        return None


def run_bundle_poller(
    control_plane_url: str,
    agent_secret: str,
    policy_dir: str | Path | None,
    *,
    interval_s: float = 30.0,
    engine: PolicyEngine | None = None,
    pep_id: str | None = None,
    version: str = "",
    mode: str = "",
    stop_event: threading.Event | None = None,
    private_key: Ed25519PrivateKey | None = None,
    key_path: str | Path | None = None,
    persist_to_disk: bool = True,
    on_bundle: Callable[[dict[str, str]], None] | None = None,
) -> None:
    """Blocking poll loop -- run this in a daemon thread, same shape as
    parapetai_gateway.server.main._watch(). `stop_event` (a threading.Event) is
    test-only: pass one and .set() it to exit the loop instead of running
    forever.

    `engine`, when given, is passed straight through to poll_once() each
    cycle -- a fetched bundle is applied to it directly (PolicyEngine.
    load_from_bundle()), not just written to disk, so this loop alone is
    sufficient for live updates; no separate watcher thread is required
    for the control-plane-driven path. `persist_to_disk` (default True,
    same meaning as poll_once's own) is threaded straight through each
    cycle -- False means every cycle applies to `engine` only, `policy_dir`
    is never touched. Also sends a heartbeat once per
    cycle when `engine` and `pep_id` are both given -- one thread, one
    interval, for both duties. `engine.status` is read AFTER poll_once()
    now genuinely reflects that same cycle's apply (not a separate,
    possibly-lagging thread's), so the heartbeat's policy_generation/digest
    are always current as of this cycle.

    private_key, when given, signs both the bundle-pull and the heartbeat
    each cycle -- callers get this from ensure_pep_identity() once, before
    starting this loop (see parapetai_gateway.server.main / parapetai_agent.maf's
    build_middleware for the two real call sites), not generated fresh
    here every cycle.

    An operator's control-plane-initiated "trigger rotation"
    (parapetai_control/agent_auth.py) arrives as `rotate_key: true` in a heartbeat's
    response body -- checked only when BOTH private_key and key_path are
    given (key_path is where the new key actually gets written; without
    it there's nowhere safe to persist a rotated identity, so the signal
    is logged and left for a future cycle rather than acted on with a
    key that would vanish on restart). Rotating swaps this loop's local
    key for every following cycle -- it does not restart the loop or
    otherwise interrupt the bundle-pull side."""
    stop_event = stop_event or threading.Event()
    last_digest: str | None = None
    current_key = private_key
    while not stop_event.is_set():
        last_digest = poll_once(
            control_plane_url,
            agent_secret,
            policy_dir,
            last_digest,
            engine=engine,
            private_key=current_key,
            persist_to_disk=persist_to_disk,
            on_bundle=on_bundle,
        )
        if engine is not None and pep_id:
            status = engine.status
            hb_result = send_heartbeat(
                control_plane_url,
                agent_secret,
                pep_id=pep_id,
                version=version,
                policy_generation=status["generation"],
                bundle_digest=status["digest"],
                mode=mode,
                private_key=current_key,
            )
            if hb_result and hb_result.get("rotate_key") and current_key is not None:
                if key_path is not None:
                    log.info("key_rotation_requested_by_control_plane")
                    current_key = pep_identity.rotate_keypair(Path(key_path))
                    register_public_key(control_plane_url, agent_secret, current_key)
                else:
                    log.warning("key_rotation_requested_but_no_key_path_configured")
        stop_event.wait(interval_s)
