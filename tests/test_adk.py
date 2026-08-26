"""Real code tests for parapetai-agent/src/parapetai_agent/adk.py.

Needs the optional `adk` extra (google-adk):
    uv run --extra adk pytest parapetai-agent/tests/test_adk.py -v

One testing strategy here, not two: synthetic-context tests only, same
"no live upstream needed" spirit as test_maf.py's own
TestChatMiddlewareSyntheticContext tier. There is no live end-to-end tier
in this file yet (contrast test_maf.py's TestToolSourcesLiveEndToEnd,
which drives a real Agent.run() against conformance/fake-upstream) --
conformance/fake-upstream/app.py is OpenAI-shaped only (no
":generateContent" route), so a real ADK Runner.run_async() call has
nowhere to point without either a real Gemini API key or extending
fake-upstream itself; extending it is a conformance/gateway concern, out
of scope for this in-process module. A documented gap, not a silent one.

ADK's own Context (what CallbackContext/ToolContext both alias to) is
constructed from a full InvocationContext, which in turn wants a real
SessionService/Session/Agent -- heavier to hand-build than
agent_framework's ChatContext/FunctionInvocationContext (test_maf.py
constructs those directly). ParapetPlugin's callback methods only ever
read a small, stable subset of Context's surface (invocation_id, user_id,
run_config.streaming_mode, function_call_id) -- see adk.py's own
signatures. The fakes below duck-type exactly that subset, same
reasoning maf.py's own _grounding_source duck-types over agent_framework
message shapes rather than importing every possible concrete type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# The core gateway test suite (`make test`) must stay independent of this
# optional extra (CLAUDE.md: "interop is never a runtime dependency of the
# core gateway") -- skip this whole file at collection time rather than
# erroring make test's default run when google-adk isn't installed.
pytest.importorskip("google.adk")

from google.adk.agents import Agent as AdkAgent
from google.adk.agents.run_config import StreamingMode
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.sessions import InMemorySessionService
from google.genai import types

from parapetai_agent.adk import (
    GovernedRunner,
    InMemoryGovernedRunner,
    ParapetPlugin,
    _declared_tools,
    _extract_texts,
    build_plugin,
    current_identity,
    provider_for_request,
    reset_plugin_registry,
    track_tool_denials,
)
from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.identity import Caller
from parapetai_agent.policy.engine import PolicyEngine

# parents[1], not [2]: this repo is <root>/tests/, whereas the platform copy
# this was ported from sat at <root>/parapetai-agent/tests/ -- one level deeper.
REPO_ROOT = Path(__file__).resolve().parents[1]
POLICIES = REPO_ROOT / "policies"


@pytest.fixture(autouse=True)
def _clean_plugin_registry():
    """Same reasoning as test_maf.py's own _clean_middleware_registry --
    build_plugin() caches per identity key and can start a background sync
    thread on a cache miss."""
    reset_plugin_registry()
    yield
    reset_plugin_registry()


def _engine_and_caller(agent_id: str = "adk-test-agent") -> tuple[PolicyEngine, Caller]:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    caller = Caller(agent_id=agent_id, tenant="default")
    return engine, caller


def _custom_policy_dir(tmp_path: Path, *cedar_snippets: str) -> Path:
    """Same shape as test_maf.py's own helper -- base permits for both
    actions plus whatever @stage/@action-annotated snippets a test needs,
    isolated from policies/ (the real, shared bundle)."""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text(
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
    )
    for i, snippet in enumerate(cedar_snippets):
        (policy_dir / f"1{i}-extra.cedar").write_text(snippet)
    return policy_dir


class _FakeRunConfig:
    def __init__(self, streaming_mode: Any) -> None:
        self.streaming_mode = streaming_mode


class _FakeCallbackContext:
    """Duck-typed stand-in for google.adk.agents.callback_context.CallbackContext."""

    def __init__(
        self, *, invocation_id: str, user_id: str | None = None, streaming: bool = False
    ) -> None:
        self.invocation_id = invocation_id
        self.user_id = user_id
        self.run_config = _FakeRunConfig(StreamingMode.SSE if streaming else StreamingMode.NONE)


class _FakeToolContext:
    """Duck-typed stand-in for google.adk.tools.tool_context.ToolContext."""

    def __init__(
        self, *, invocation_id: str, function_call_id: str = "call-1", user_id: str | None = None
    ) -> None:
        self.invocation_id = invocation_id
        self.function_call_id = function_call_id
        self.user_id = user_id


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _llm_request(text: str = "hello", *, tools: list[str] | None = None) -> LlmRequest:
    config = types.GenerateContentConfig()
    if tools:
        config.tools = [
            types.Tool(
                function_declarations=[types.FunctionDeclaration(name=name) for name in tools]
            )
        ]
    return LlmRequest(
        model="gemini-2.5-flash",
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=config,
    )


def _llm_response(text: str, *, partial: bool = False) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        partial=partial,
    )


class TestProviderIdentification:
    def test_always_gemini(self) -> None:
        assert provider_for_request(_llm_request()) == "gemini"


class TestSnapshotBuilders:
    def test_extract_texts_walks_content_and_lists(self) -> None:
        content = types.Content(
            role="user", parts=[types.Part(text="hello"), types.Part(text="world")]
        )
        assert _extract_texts(content) == ["hello", "world"]
        assert _extract_texts("plain string") == ["plain string"]
        assert _extract_texts([content, "extra"]) == ["hello", "world", "extra"]
        assert _extract_texts(None) == []

    def test_declared_tools_walks_function_declarations(self) -> None:
        config = types.GenerateContentConfig(
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(name="get_weather"),
                        types.FunctionDeclaration(name="lookup_order"),
                    ]
                )
            ]
        )
        assert _declared_tools(config) == ["get_weather", "lookup_order"]
        assert _declared_tools(None) == []


class TestModelCallSyntheticContext:
    async def test_pre_call_allow_returns_none(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-1")

        resp = await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request())

        assert resp is None

    async def test_pre_call_deny_returns_synthetic_response_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path, 'forbid(principal, action == Action::"model_call", resource);'
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="pre-deny-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-2")

        resp = await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request())

        assert resp is not None
        assert resp.error_code == "governance_denied"
        assert resp.content is not None
        assert "GOVERNANCE_DENIED" in (resp.content.parts[0].text or "")
        # Cleaned up so a later after_model_callback for the same
        # invocation (if ADK still fires it after an early exit) finds
        # nothing to double-process -- see before_model_callback's own
        # comment on why.
        assert "inv-2" not in plugin._model_correlations

    async def test_post_call_deny_blocks_a_non_streaming_response(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-deny-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-3")

        pre = await plugin.before_model_callback(
            callback_context=ctx, llm_request=_llm_request("what is my SSN")
        )
        assert pre is None

        resp = await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("your SSN is 123-45-6789")
        )
        assert resp is not None
        assert resp.error_code == "governance_denied"

    async def test_post_call_allows_a_clean_response(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-allow-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-4")

        await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request("hello"))
        resp = await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("hi there, how can I help?")
        )
        assert resp is None

    async def test_after_model_callback_with_no_matching_before_is_a_noop(self) -> None:
        """Defensive branch: after_model_callback firing for an
        invocation_id this plugin never saw a before_model_callback for
        (e.g. it already denied and cleaned up) must not raise."""
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="never-seen")

        resp = await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("hi")
        )
        assert resp is None


class TestStreamingAccumulation:
    """Confirms this module's documented streaming design (adk.py's own
    module docstring's "Streaming" section): every partial=True chunk
    relays unmodified, and the post-call Cedar decision runs once, against
    the ACCUMULATED text, only on the final partial=False chunk."""

    async def test_partial_chunks_relay_unmodified_final_chunk_evaluated(
        self, tmp_path: Path
    ) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="stream-deny-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-5", streaming=True)

        await plugin.before_model_callback(
            callback_context=ctx, llm_request=_llm_request("what is my SSN")
        )

        chunk1 = await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("your SSN ", partial=True)
        )
        assert chunk1 is None  # relayed unmodified -- no evaluation on a partial chunk

        # Neither chunk alone matches "*SSN*...digits*" -- only the
        # ACCUMULATED text ("your SSN is 123-45-6789") does.
        chunk2 = await plugin.after_model_callback(
            callback_context=ctx, llm_response=_llm_response("is 123-45-6789", partial=False)
        )
        assert chunk2 is not None
        assert chunk2.error_code == "governance_denied"

    async def test_partial_chunks_allowed_when_accumulated_text_is_clean(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-6", streaming=True)

        await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request("hello"))
        assert (
            await plugin.after_model_callback(
                callback_context=ctx, llm_response=_llm_response("hi ", partial=True)
            )
            is None
        )
        assert (
            await plugin.after_model_callback(
                callback_context=ctx, llm_response=_llm_response("there", partial=False)
            )
            is None
        )
        # Correlation (and its open span) cleaned up after the final chunk.
        assert "inv-6" not in plugin._model_correlations


class TestToolCallSyntheticContext:
    async def test_before_tool_callback_allow_returns_none(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeToolContext(invocation_id="inv-7")

        resp = await plugin.before_tool_callback(
            tool=_FakeTool("get_weather"), tool_args={"city": "nyc"}, tool_context=ctx
        )
        assert resp is None

    async def test_before_tool_callback_deny_returns_result_dict_and_is_tracked(
        self, tmp_path: Path
    ) -> None:
        """A denied before_tool_callback returns a RESULT dict (per
        BasePlugin's own docstring: "it will stop the tool execution and
        return this response immediately"), not modified args -- confirmed
        against google-adk 2.7's own BasePlugin source, not assumed."""
        policy_dir = _custom_policy_dir(
            tmp_path, 'forbid(principal, action == Action::"tool_call", resource);'
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-pre-deny-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeToolContext(invocation_id="inv-8")

        with track_tool_denials() as denials:
            resp = await plugin.before_tool_callback(
                tool=_FakeTool("execute_shell"), tool_args={"cmd": "rm -rf /"}, tool_context=ctx
            )
        assert resp is not None
        assert "GOVERNANCE_DENIED" in resp["error"]
        # Deterministically observable regardless of what a model does
        # with resp -- same track_tool_denials() contract as maf.py's.
        assert denials

    async def test_after_tool_callback_allow_returns_none(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeToolContext(invocation_id="inv-9")

        await plugin.before_tool_callback(
            tool=_FakeTool("get_weather"), tool_args={}, tool_context=ctx
        )
        post = await plugin.after_tool_callback(
            tool=_FakeTool("get_weather"),
            tool_args={},
            tool_context=ctx,
            result={"forecast": "sunny"},
        )
        assert post is None

    async def test_after_tool_callback_deny(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@stage("post")\n'
            'forbid(principal, action == Action::"tool_call", resource)\n'
            "when { context has tool_result_preview && "
            'context.tool_result_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-post-deny-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeToolContext(invocation_id="inv-10")

        pre = await plugin.before_tool_callback(
            tool=_FakeTool("lookup_secret"), tool_args={}, tool_context=ctx
        )
        assert pre is None

        post = await plugin.after_tool_callback(
            tool=_FakeTool("lookup_secret"),
            tool_args={},
            tool_context=ctx,
            result={"value": "the secret is 42"},
        )
        assert post is not None
        assert "GOVERNANCE_DENIED" in post["error"]

    async def test_after_tool_callback_alter(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@stage("post") @alter_with("redact_all")\n'
            'permit(principal, action == Action::"tool_call", resource)\n'
            "when { context has tool_result_preview && "
            'context.tool_result_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-alter-test", tenant="default")
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeToolContext(invocation_id="inv-11")

        await plugin.before_tool_callback(
            tool=_FakeTool("lookup_secret"), tool_args={}, tool_context=ctx
        )
        post = await plugin.after_tool_callback(
            tool=_FakeTool("lookup_secret"),
            tool_args={},
            tool_context=ctx,
            result={"value": "the secret is 42"},
        )
        assert post == "[REDACTED BY POLICY]"


class TestIdentityResolution:
    async def test_session_user_id_is_not_used_by_default(self) -> None:
        """Session.user_id is unverified -- ADK requires it unconditionally
        (Runner.run_async() has no optional path), so folding it into
        identity_claims by default would make identity-gated Cedar policies
        silently stricter for ADK than for MAF on the same policy bundle.
        See adk.py's own module docstring's "Identity" section for the
        verified-live finding this default is based on."""
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller)
        ctx = _FakeCallbackContext(invocation_id="inv-12", user_id="bob")

        await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request())

        correlation = plugin._model_correlations["inv-12"]
        assert correlation.identity_claims == {}

    async def test_session_user_id_is_a_fallback_claim_when_trusted(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller, trust_session_user_id=True)
        ctx = _FakeCallbackContext(invocation_id="inv-12b", user_id="bob")

        await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request())

        correlation = plugin._model_correlations["inv-12b"]
        assert correlation.identity_claims == {"sub": "bob"}

    async def test_ambient_identity_wins_over_session_user_id(self) -> None:
        engine, caller = _engine_and_caller()
        plugin = ParapetPlugin(engine, caller, trust_session_user_id=True)
        ctx = _FakeCallbackContext(invocation_id="inv-13", user_id="bob")

        with current_identity(claims={"oid": "real-verified-user"}, roles=["Admin"]):
            await plugin.before_model_callback(callback_context=ctx, llm_request=_llm_request())

        correlation = plugin._model_correlations["inv-13"]
        assert correlation.identity_claims == {"oid": "real-verified-user"}
        assert correlation.identity_roles == ["Admin"]

    def test_identity_api_is_the_exact_same_objects_maf_exports(self) -> None:
        """governed_identity()/current_identity() are literally the same
        objects parapetai_agent.maf exports -- an app switching frameworks
        changes zero identity code. Checked against parapetai_agent.
        scoped_data directly (not maf.py) to keep this test file
        independent of the `maf` extra."""
        import parapetai_agent.scoped_data as scoped_data
        from parapetai_agent.adk import current_identity as adk_current_identity
        from parapetai_agent.adk import governed_identity as adk_governed_identity
        from parapetai_agent.adk import identity_from_bearer_token as adk_token_identity

        assert adk_current_identity is scoped_data.current_identity
        assert adk_governed_identity is scoped_data.governed_identity
        assert adk_token_identity is scoped_data.identity_from_bearer_token


class TestSessionUserIdMatchesMafDefaultPosture:
    """Regression test for a real cross-framework parity bug found live:
    with trust_session_user_id defaulted on, ADK's own Session.user_id
    (mandatory, unverified) satisfied policies/30-identity.cedar's
    `context has identity_claims` guard on every single call, silently
    denying `lookup_order` for a caller that never asserted any identity
    at all -- something MAF (identity fully optional) would have allowed
    on the exact same policy bundle. See adk.py's module docstring's
    "Identity" section. Uses the real, shared policies/ dir (not a
    synthetic one) specifically to prove this against the actual bundle
    a real deployment would run, not a hand-picked test fixture."""

    async def test_session_user_id_alone_does_not_trip_the_order_viewer_gate(
        self,
    ) -> None:
        engine, caller = _engine_and_caller(agent_id="parity-test-untrusted")
        plugin = ParapetPlugin(engine, caller)  # trust_session_user_id defaults False
        ctx = _FakeToolContext(invocation_id="parity-inv-1", user_id="alice")

        resp = await plugin.before_tool_callback(
            tool=_FakeTool("lookup_order"), tool_args={"order_id": "123"}, tool_context=ctx
        )

        # No identity asserted at all (from Cedar's point of view) -> the
        # role-gate rule doesn't apply -> same as MAF's own default.
        assert resp is None

    async def test_session_user_id_alone_DOES_trip_the_gate_when_trusted(self) -> None:
        """The opt-in still works as documented -- this is a deliberate
        choice a deployment can make, not a broken feature."""
        engine, caller = _engine_and_caller(agent_id="parity-test-trusted")
        plugin = ParapetPlugin(engine, caller, trust_session_user_id=True)
        ctx = _FakeToolContext(invocation_id="parity-inv-2", user_id="alice")

        resp = await plugin.before_tool_callback(
            tool=_FakeTool("lookup_order"), tool_args={"order_id": "123"}, tool_context=ctx
        )

        assert resp is not None
        assert "GOVERNANCE_DENIED" in resp["error"]


class TestTier2ContentChecksEnforcement:
    """ParapetPlugin(content_checks=...) -- same tier-2 "parse + decide"
    path as maf.py's own ParapetChatMiddleware(content_checks=...), a real
    scanner running against the outgoing prompt BEFORE Cedar."""

    async def test_prompt_containing_ssn_is_denied(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);\n'
        )
        (policy_dir / "45-tier2.cedar").write_text(
            'forbid (principal, action == Action::"model_call", resource)\n'
            "when {\n"
            "  context has content_checks_pii_types &&\n"
            '  context.content_checks_pii_types.containsAny(["US_SSN"])\n'
            "};\n"
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-adk-test", tenant="default")
        content_checks = ContentCheckConfig()
        content_checks.load_from_bundle(
            {
                "content_checks.json": (
                    '[{"library_id": "pii-ssn", "scanner_id": "regex_entities", '
                    '"entity_types": ["US_SSN"], "context_key": "content_checks_pii_types"}]'
                )
            }
        )
        plugin = ParapetPlugin(engine, caller, content_checks=content_checks)
        ctx = _FakeCallbackContext(invocation_id="inv-14")

        resp = await plugin.before_model_callback(
            callback_context=ctx,
            llm_request=_llm_request("my SSN is 123-45-6789, please help me file taxes"),
        )
        assert resp is not None
        assert resp.error_code == "governance_denied"

    async def test_clean_prompt_is_allowed(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);\n'
        )
        (policy_dir / "45-tier2.cedar").write_text(
            'forbid (principal, action == Action::"model_call", resource)\n'
            "when {\n"
            "  context has content_checks_pii_types &&\n"
            '  context.content_checks_pii_types.containsAny(["US_SSN"])\n'
            "};\n"
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-adk-allow-test", tenant="default")
        content_checks = ContentCheckConfig()
        content_checks.load_from_bundle(
            {
                "content_checks.json": (
                    '[{"library_id": "pii-ssn", "scanner_id": "regex_entities", '
                    '"entity_types": ["US_SSN"], "context_key": "content_checks_pii_types"}]'
                )
            }
        )
        plugin = ParapetPlugin(engine, caller, content_checks=content_checks)
        ctx = _FakeCallbackContext(invocation_id="inv-15")

        resp = await plugin.before_model_callback(
            callback_context=ctx, llm_request=_llm_request("what's the weather like today?")
        )
        assert resp is None


class TestBuildPluginBundledDefault:
    def test_bundled_default_policy_permits_a_model_call(self) -> None:
        plugin = build_plugin(agent_id="bundled-default-adk-test")
        decision = plugin.engine.evaluate(
            principal='Agent::"bundled-default-adk-test"',
            action="model_call",
            resource="test",
            context={},
        )
        assert decision.allowed


class TestBuildPluginIdentityRegistry:
    """build_plugin() is idempotent per (policy_dir, entities_path,
    agent_id, tenant, control_plane_url) -- same contract as
    maf.build_middleware(), verified the same way."""

    def test_repeated_calls_for_same_identity_reuse_the_same_plugin(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(tmp_path)
        plugin1 = build_plugin(policy_dir, agent_id="reuse-test")
        plugin2 = build_plugin(policy_dir, agent_id="reuse-test")
        assert plugin1 is plugin2

    def test_different_agent_id_gets_its_own_plugin(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(tmp_path)
        plugin1 = build_plugin(policy_dir, agent_id="reuse-test-a")
        plugin2 = build_plugin(policy_dir, agent_id="reuse-test-b")
        assert plugin1 is not plugin2


class TestGovernedRunner:
    def test_injects_the_plugin_alongside_any_explicit_plugins(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(tmp_path)
        agent = AdkAgent(name="test_agent", model="gemini-2.5-flash", instruction="be helpful")
        extra = LoggingPlugin()

        runner = GovernedRunner(
            app_name="test_app",
            agent=agent,
            session_service=InMemorySessionService(),
            policy_dir=policy_dir,
            agent_id="governed-runner-test",
            plugins=[extra],
        )

        parapet_plugin = runner.plugin_manager.get_plugin("parapetai")
        assert isinstance(parapet_plugin, ParapetPlugin)
        assert parapet_plugin.caller.agent_id == "governed-runner-test"
        assert runner.plugin_manager.get_plugin("logging_plugin") is extra

    def test_injects_into_an_app_s_own_plugins_list(self, tmp_path: Path) -> None:
        """google-adk 2.7 deprecates Runner(plugins=[...]) in favor of
        Runner(app=App(..., plugins=[...])) -- and raises ValueError if
        BOTH app= and plugins= are passed (confirmed against
        Runner._resolve_app() source). GovernedRunner must branch on this
        rather than always setting kwargs["plugins"]."""
        from google.adk.apps import App

        policy_dir = _custom_policy_dir(tmp_path)
        agent = AdkAgent(name="test_agent", model="gemini-2.5-flash", instruction="be helpful")
        extra = LoggingPlugin()
        app = App(name="test_app", root_agent=agent, plugins=[extra])

        runner = GovernedRunner(
            app=app,
            session_service=InMemorySessionService(),
            policy_dir=policy_dir,
            agent_id="governed-runner-app-test",
        )

        parapet_plugin = runner.plugin_manager.get_plugin("parapetai")
        assert isinstance(parapet_plugin, ParapetPlugin)
        assert runner.plugin_manager.get_plugin("logging_plugin") is extra


class TestInMemoryGovernedRunner:
    """InMemoryGovernedRunner -- the InMemoryRunner-shaped convenience real
    ADK samples commonly reach for (e.g. google/adk-samples'
    safety-plugins/main.py: `InMemoryRunner(agent=..., plugins=[...])`),
    which bare GovernedRunner doesn't mirror on its own."""

    def test_defaults_match_in_memory_runner(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(tmp_path)
        agent = AdkAgent(name="test_agent", model="gemini-2.5-flash", instruction="be helpful")

        runner = InMemoryGovernedRunner(agent=agent, policy_dir=policy_dir, agent_id="inmem-test")

        assert runner.app_name == "InMemoryRunner"  # same default InMemoryRunner itself uses
        assert type(runner.session_service).__name__ == "InMemorySessionService"
        assert type(runner.artifact_service).__name__ == "InMemoryArtifactService"
        assert type(runner.memory_service).__name__ == "InMemoryMemoryService"
        parapet_plugin = runner.plugin_manager.get_plugin("parapetai")
        assert isinstance(parapet_plugin, ParapetPlugin)
        assert parapet_plugin.caller.agent_id == "inmem-test"

    def test_explicit_app_name_overrides_the_default(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(tmp_path)
        agent = AdkAgent(name="test_agent", model="gemini-2.5-flash", instruction="be helpful")

        runner = InMemoryGovernedRunner(
            agent=agent, app_name="my-app", policy_dir=policy_dir, agent_id="inmem-test-2"
        )

        assert runner.app_name == "my-app"

    def test_explicit_session_service_overrides_the_default(self, tmp_path: Path) -> None:
        from google.adk.sessions import InMemorySessionService

        policy_dir = _custom_policy_dir(tmp_path)
        agent = AdkAgent(name="test_agent", model="gemini-2.5-flash", instruction="be helpful")
        own_session_service = InMemorySessionService()

        runner = InMemoryGovernedRunner(
            agent=agent,
            session_service=own_session_service,
            policy_dir=policy_dir,
            agent_id="inmem-test-3",
        )

        assert runner.session_service is own_session_service
