"""Shared fixtures for parapetai-agent/tests.

parapetai_agent.identity_store's default store is genuine module-level state
(configure_identity_store() is documented as "call once, at process
startup" -- see that function's docstring) -- without a reset here, a key
set by one test file's test would still be visible to the next test FILE
in the same pytest process, not just the next test in the same file.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from parapetai_agent.identity_store import InMemoryIdentityStore, configure_identity_store


@pytest.fixture(autouse=True)
def _reset_identity_store():
    configure_identity_store(InMemoryIdentityStore())
    yield


@pytest.fixture(autouse=True)
def _reset_otel_module_state() -> Iterator[None]:
    """build_middleware() now calls configure_otel() itself whenever a
    control plane is configured (see maf.py's own docstring on this --
    "OpenTelemetry is wired up automatically too"), so ANY test that
    passes control_plane_url/agent_secret (or sets the PARAPETAI_* env var
    fallbacks) can trigger a REAL, process-wide TracerProvider/
    LoggerProvider registration now, not just tests that mean to
    exercise OTel directly. Without resetting it after every test, one
    test's build_middleware(control_plane_url=...) call leaves a real
    provider registered, which silently breaks a LATER test expecting
    the untouched no-op default (parapetai_agent.maf's own module-level
    `_tracer = trace.get_tracer(__name__)` is a lazy proxy that resolves
    against whatever is globally registered at call time) -- and, worse,
    a stopped BatchSpanProcessor worker thread whose queue never drains
    can hang a later test outright. Both were real failures hit live
    while building this feature. Directly resetting opentelemetry.trace's
    own internal _TRACER_PROVIDER/_TRACER_PROVIDER_SET_ONCE globals is
    reaching into OTel's private state, but there is no public "unset"
    API and no cleaner seam available."""
    import parapetai_agent.maf as maf_module

    yield
    maf_module._otel_tracer_provider = None
    maf_module._otel_logger_provider = None
    maf_module._otel_logger = None

    import opentelemetry.trace as trace_api
    from opentelemetry.util._once import Once

    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
