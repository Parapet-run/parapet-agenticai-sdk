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
def _reset_govern_span_state() -> Iterator[None]:
    """parapetai_agent.govern's _current_model_span/_current_tool_span
    (auth-integrations.md §10.7) are real _OpenSpan objects holding an
    opentelemetry.context.attach() token -- module-level contextvar state
    with no natural close point of its own (see _OpenSpan's own
    docstring), same class of leak-across-tests risk
    _reset_otel_module_state below already guards against for OTel's
    provider globals. Confirmed live: an async @gov.tool invocation in one
    test can leave a token whose Context does not match a LATER test's own
    Context (a genuine cross-Context attach/detach mismatch, not just a
    hygiene nicety) -- _OpenSpan.close() already tolerates that itself
    (never raises), but without this reset the STALE span/framework tag
    stays "current" and leaks into whichever test runs next, same
    observable failure mode _reset_otel_module_state's own docstring
    describes for a stale TracerProvider."""
    yield
    import parapetai_agent.govern as govern_module
    import parapetai_agent.observation as observation_module

    for var in (govern_module._current_model_span, govern_module._current_tool_span):
        open_span = var.get()
        if open_span is not None:
            open_span.close()
            var.set(None)
    # Belt and suspenders: open_span.close() above calls
    # framework_scope.reset(), which itself tolerates (but may not
    # SUCCEED at, across a genuine cross-Context mismatch -- see
    # FrameworkScope.reset()'s own docstring) restoring the prior value.
    # ContextVar.set() (unlike .reset(token)) has no such restriction, so
    # this unconditionally clears it for whatever context the NEXT test
    # runs in, regardless of whether the token-based reset above actually
    # took effect.
    observation_module._current_framework.set(None)


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
    API and no cleaner seam available.

    The provider state itself lives in parapetai_agent.governance_runtime,
    not parapetai_agent.maf -- shared by every in-process framework
    integration (parapetai_agent.maf, parapetai_agent.adk, ...) so that
    "has this process already configured OTel" is one answer, not one per
    framework module. See that module's own docstring."""
    import parapetai_agent.governance_runtime as gr_module

    yield
    gr_module._otel_tracer_provider = None
    gr_module._otel_logger_provider = None
    gr_module._otel_logger = None

    import opentelemetry.trace as trace_api
    from opentelemetry.util._once import Once

    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = Once()
