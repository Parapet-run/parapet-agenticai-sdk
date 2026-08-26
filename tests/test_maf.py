"""Real code tests for parapetai-agent/src/parapetai_agent/maf.py -- backs the
"verify through a code test" requirement behind parapetai-support.yaml.

Needs the optional `maf` extra (agent_framework, mcp):
    uv run --with agent-framework --with "mcp==1.29.0" pytest parapetai-agent/tests/test_maf.py -v

Two testing strategies, deliberately kept separate:

  - Synthetic-context tests (TestProviderIdentification, TestChatMiddleware
    unit-level cases): construct a real ChatContext/FunctionInvocationContext
    directly with a real client instance, call middleware.process() directly
    with a no-op call_next. No live upstream needed -- this is how
    anthropic/gemini/azure provider identification is verified without live
    credentials for any of them.

  - Live end-to-end tests (TestToolSourcesLiveEndToEnd): drive a real
    Agent.run() against conformance/fake-upstream (deterministic, no
    credentials) and, for MCP sources, the real
    conformance/mcp-probe/server.py (reused unmodified, same as
    spike/maf_mcp_check/). These prove the ContextVar correlation and Cedar
    enforcement work when driven by MAF's real machinery, not just
    hand-built contexts.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

# The core gateway test suite (`make test`) must stay independent of this
# optional extra (CLAUDE.md: "interop is never a runtime dependency of the
# core gateway") -- skip this whole file at collection time rather than
# erroring make test's default run when agent_framework isn't installed.
# Run via: uv run --with agent-framework --with "mcp==1.29.0" pytest
# parapetai-agent/tests/test_maf.py -v
pytest.importorskip("agent_framework")

import respx
from agent_framework import (
    Agent,
    ChatContext,
    ChatResponse,
    FunctionInvocationContext,
    FunctionTool,
    Message,
)
from agent_framework.anthropic import AnthropicClient
from agent_framework.foundry import FoundryChatClient
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient
from azure.identity import AzureCliCredential
from httpx import Response

from parapetai_agent.identity import ANONYMOUS, Caller
from parapetai_agent.maf import (
    GovernedAgent,
    ParapetChatMiddleware,
    ParapetFunctionMiddleware,
    _effective_principal,
    _identity_claims,
    _identity_roles,
    agent_identity,
    build_middleware,
    configure_rotating_audit_log,
    current_identity,
    governed_identity,
    identity_from_azure_credential,
    identity_from_bearer_token,
    provider_for_client,
    reset_middleware_registry,
)
from parapetai_agent.policy.engine import PolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICIES = REPO_ROOT / "policies"


@pytest.fixture(autouse=True)
def _clean_middleware_registry():
    """build_middleware() now caches per (policy_dir, entities_path,
    agent_id, tenant, control_plane_url) and starts a background sync
    thread on a cache miss (see reset_middleware_registry()'s own
    docstring). Without this, a test using a fixed policy_dir + agent_id
    combination the SAME way a real long-running app would (deliberately,
    to prove reuse -- see TestBuildMiddlewareIdentityRegistry below) would
    leak its cached engine and thread into whichever test happens to run
    next with the same key, and every control-plane-configured test would
    leave a daemon thread polling a URL that's only mocked for the
    duration of that one test. Reset before AND after: before, so a
    previous test's state (if this fixture were ever skipped somewhere)
    can't leak in; after, so this test's own thread stops immediately
    rather than lingering until the whole process exits."""
    reset_middleware_registry()
    yield
    reset_middleware_registry()


async def _noop_call_next() -> None:
    return None


def _engine_and_caller() -> tuple[PolicyEngine, Caller]:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    caller = Caller(agent_id="maf-test-agent", tenant="default")
    return engine, caller


# ── provider identification, verified against real client classes ─────────


class TestProviderIdentification:
    def test_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4")
        client = OpenAIChatCompletionClient()
        assert provider_for_client(client) == "openai"

    def test_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        monkeypatch.setenv("ANTHROPIC_CHAT_MODEL_ID", "claude-3")
        client = AnthropicClient()
        assert provider_for_client(client) == "anthropic"

    def test_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-pro")
        client = GeminiChatClient()
        assert provider_for_client(client) == "gemini"

    def test_azure_openai_is_not_the_openai_class_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for a real mistake made while building this:
        Azure OpenAI uses the SAME OpenAIChatCompletionClient class as plain
        OpenAI -- type(client).__name__ alone can't tell them apart. Verified
        live that .azure_endpoint is the actual distinguishing attribute."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_CHAT_COMPLETION_MODEL", raising=False)
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("AZURE_OPENAI_CHAT_COMPLETION_MODEL", "gpt-4")
        client = OpenAIChatCompletionClient()
        assert type(client).__name__ == "OpenAIChatCompletionClient"  # same class as plain OpenAI
        assert provider_for_client(client) == "azure"  # but correctly distinguished

    def test_unrecognised_client_maps_to_unknown_not_guessed(self) -> None:
        class SomeFutureClient:
            pass

        assert provider_for_client(SomeFutureClient()) == "unknown"

    def test_foundry(self) -> None:
        """FoundryChatClient falls through .azure_endpoint (unset -- it's
        None, confirmed live) to the class-name table, which maps it to
        the same "azure" bucket as Azure-configured OpenAIChatCompletionClient
        (see _PROVIDER_BY_CLIENT_CLASS's own comment for why one bucket,
        not a distinct "foundry" one). AzureCliCredential resolves lazily
        on first real call, not at construction -- confirmed live this
        needs no `az login` or real project to construct."""
        client = FoundryChatClient(
            project_endpoint="https://example.services.ai.azure.com",
            model="gpt-4o",
            credential=AzureCliCredential(),
        )
        assert getattr(client, "azure_endpoint", None) is None
        assert provider_for_client(client) == "azure"


# ── chat middleware: synthetic context, real Cedar decisions ───────────────


class TestChatMiddlewareSyntheticContext:
    async def test_allowed_model_call_calls_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )
        called = {"next": False}

        async def call_next() -> None:
            called["next"] = True

        await mw.process(ctx, call_next)
        assert called["next"] is True

    async def test_denied_model_call_raises_and_never_calls_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """untrusted_tier_restricted_models forbids opus models for
        untrusted trust_tier -- real existing policy, no test-only rule."""
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "claude-opus-4")
        engine, _ = _engine_and_caller()
        caller = Caller(agent_id="maf-test-agent", tenant="default", trust_tier="untrusted")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "claude-opus-4", "tools": []},
        )
        called = {"next": False}

        async def call_next() -> None:
            called["next"] = True

        from parapetai_agent.maf import GovernanceDenied

        with pytest.raises(GovernanceDenied):
            await mw.process(ctx, call_next)
        assert called["next"] is False


class TestTier2ContentChecksEnforcement:
    """ParapetChatMiddleware(content_checks=...) -- the actual tier-2
    "parse + decide" path this feature exists for: a real scanner runs
    against the outgoing prompt BEFORE Cedar, and its output is what a
    generated Cedar rule (control-plane's 45-content-checks-tier2.cedar
    in real deployments; a hand-built equivalent here) gates on. Uses the
    same synthetic-ChatContext strategy as TestChatMiddlewareSyntheticContext
    above -- no live upstream needed."""

    @staticmethod
    def _policy_dir_gating_on_pii_types(tmp_path: Path) -> Path:
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "00-base.cedar").write_text(
            'permit(principal, action == Action::"model_call", resource);\n'
            'permit(principal, action == Action::"tool_call", resource);\n'
        )
        # Same shape control-plane's content_checks.py renders for a real
        # tier-2 selection: gated on `context has content_checks` (safe
        # ONLY because a scanner failure denies before this rule is ever
        # reached -- see content_checks.py's own module docstring).
        (policy_dir / "45-tier2.cedar").write_text(
            '@id("content-check-pii-ssn")\n'
            'forbid (principal, action == Action::"model_call", resource)\n'
            "when {\n"
            "  context has content_checks_pii_types &&\n"
            '  context.content_checks_pii_types.containsAny(["US_SSN"])\n'
            "};\n"
        )
        return policy_dir

    async def test_prompt_containing_ssn_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from parapetai_agent.content_checks import ContentCheckConfig
        from parapetai_agent.maf import GovernanceDenied

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        policy_dir = self._policy_dir_gating_on_pii_types(tmp_path)
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-test-agent", tenant="default")

        content_checks = ContentCheckConfig()
        content_checks.load_from_bundle(
            {
                "content_checks.json": (
                    '[{"library_id": "pii-ssn", "scanner_id": "regex_entities", '
                    '"entity_types": ["US_SSN"], "context_key": "content_checks_pii_types"}]'
                )
            }
        )
        mw = ParapetChatMiddleware(engine, caller, content_checks=content_checks)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["my SSN is 123-45-6789, please help me file taxes"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        with pytest.raises(GovernanceDenied) as exc_info:
            await mw.process(ctx, _noop_call_next)
        # determining_policies is cedarpy's own positional id ("policy2"),
        # never the @id() annotation string, regardless of what's set --
        # see parapetai_control/policy_index.py's own module docstring for this exact
        # gotcha, verified live here too rather than assumed.
        assert exc_info.value.decision.effect == "deny"
        assert not exc_info.value.decision.allowed

    async def test_clean_prompt_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from parapetai_agent.content_checks import ContentCheckConfig

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        policy_dir = self._policy_dir_gating_on_pii_types(tmp_path)
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-test-agent", tenant="default")

        content_checks = ContentCheckConfig()
        content_checks.load_from_bundle(
            {
                "content_checks.json": (
                    '[{"library_id": "pii-ssn", "scanner_id": "regex_entities", '
                    '"entity_types": ["US_SSN"], "context_key": "content_checks_pii_types"}]'
                )
            }
        )
        mw = ParapetChatMiddleware(engine, caller, content_checks=content_checks)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["what's the weather like today?"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )
        called = {"next": False}

        async def call_next() -> None:
            called["next"] = True

        await mw.process(ctx, call_next)
        assert called["next"] is True

    async def test_no_content_checks_configured_behaves_exactly_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """content_checks=None (the default) must be a true no-op -- an
        agent with zero tier-2 selections is unaffected by this feature
        existing at all, same prompt that would trip the SSN rule above
        sails through because nothing ever populates context.content_checks."""
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        policy_dir = self._policy_dir_gating_on_pii_types(tmp_path)
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-test-agent", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)  # no content_checks
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["my SSN is 123-45-6789"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )
        called = {"next": False}

        async def call_next() -> None:
            called["next"] = True

        await mw.process(ctx, call_next)
        assert called["next"] is True

    async def test_unrunnable_scanner_denies_before_cedar_ever_evaluates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fail-closed proof this whole design turns on: a configured
        scanner that can't run (unknown scanner_id here, standing in for
        a real backend raising) must deny WITHOUT ever calling
        engine.evaluate() -- if it did call through, `context has
        content_checks` would be false and the forbid above would
        silently never fire, exactly the bypass content_checks.py's
        module docstring exists to prevent."""
        from parapetai_agent.content_checks import ContentCheckConfig
        from parapetai_agent.maf import GovernanceDenied

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        policy_dir = self._policy_dir_gating_on_pii_types(tmp_path)
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tier2-test-agent", tenant="default")

        evaluate_calls = {"count": 0}
        real_evaluate = engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            evaluate_calls["count"] += 1
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        content_checks = ContentCheckConfig()
        content_checks.load_from_bundle(
            {
                "content_checks.json": (
                    '[{"library_id": "broken", "scanner_id": "not_a_real_scanner", '
                    '"entity_types": ["US_SSN"], "context_key": "content_checks_pii_types"}]'
                )
            }
        )
        mw = ParapetChatMiddleware(engine, caller, content_checks=content_checks)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello, totally clean message"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        with pytest.raises(GovernanceDenied) as exc_info:
            await mw.process(ctx, _noop_call_next)
        assert "not_a_real_scanner" in exc_info.value.decision.reason
        assert evaluate_calls["count"] == 0


class TestModelFallbackToClientDefault:
    """Real, verified gap: a client configured with model= only at
    construction (every Foundry example in this repo; also how an
    Azure-configured OpenAI client is typically built) never puts "model"
    into per-call ChatContext.options at all -- the client injects its OWN
    default deep inside its raw _prepare_options(), after this middleware
    has already read options. _client_default_model() falls back to the
    client's own `.model` attribute, verified to be the real, consistent
    attribute name across OpenAI/Foundry/Anthropic/Gemini client classes,
    not a Foundry-specific guess."""

    async def test_falls_back_to_client_model_when_options_lack_one(self) -> None:
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import AzureCliCredential

        engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(engine, caller)
        seen_contexts: list[dict[str, object]] = []
        real_evaluate = engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            seen_contexts.append(dict(kwargs["context"]))  # type: ignore[arg-type]
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        client = FoundryChatClient(
            project_endpoint="https://example.services.ai.azure.com",
            model="gpt-4o",
            credential=AzureCliCredential(),
        )
        ctx = ChatContext(
            client=client,
            messages=[Message("user", ["hello"])],
            options={"tools": []},  # deliberately no "model" key -- the real-world shape
        )

        await mw.process(ctx, _noop_call_next)

        assert len(seen_contexts) == 1
        assert seen_contexts[0].get("model") == "gpt-4o"

    async def test_explicit_per_call_model_still_wins_over_client_default(self) -> None:
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import AzureCliCredential

        engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(engine, caller)
        seen_contexts: list[dict[str, object]] = []
        real_evaluate = engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            seen_contexts.append(dict(kwargs["context"]))  # type: ignore[arg-type]
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        client = FoundryChatClient(
            project_endpoint="https://example.services.ai.azure.com",
            model="gpt-4o",
            credential=AzureCliCredential(),
        )
        ctx = ChatContext(
            client=client,
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        await mw.process(ctx, _noop_call_next)

        assert len(seen_contexts) == 1
        assert seen_contexts[0].get("model") == "gpt-4o-mini"


# ── post-call governance: DENY/ALTER on the response, synthetic context ────


def _custom_policy_dir(tmp_path: Path, *cedar_snippets: str) -> Path:
    """Base permits for both actions plus whatever @stage/@action-annotated
    snippets a test needs, isolated from policies/ (the real, shared
    bundle) so these tests can't accidentally depend on or perturb it."""
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "00-base.cedar").write_text(
        'permit(principal, action == Action::"model_call", resource);\n'
        'permit(principal, action == Action::"tool_call", resource);\n'
    )
    for i, snippet in enumerate(cedar_snippets):
        (policy_dir / f"1{i}-extra.cedar").write_text(snippet)
    return policy_dir


class TestChatPostCallGovernance:
    async def test_post_call_deny_blocks_non_streaming_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_deny")\n@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-deny-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["what is my SSN"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )
        called = {"next": False}

        async def call_next() -> None:
            called["next"] = True
            ctx.result = ChatResponse(messages=[Message("assistant", ["your SSN is 123-45-6789"])])

        from parapetai_agent.maf import GovernanceDenied

        with pytest.raises(GovernanceDenied):
            await mw.process(ctx, call_next)
        # The model call itself already happened (pre-call allowed it) --
        # it's the RESPONSE reaching the caller that gets blocked.
        assert called["next"] is True

    async def test_post_call_allows_response_that_does_not_match_any_rule(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_deny")\n@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-allow-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", ["hi there"])])

        await mw.process(ctx, call_next)  # must not raise
        assert isinstance(ctx.result, ChatResponse)
        assert ctx.result.text == "hi there"  # untouched

    async def test_post_call_alter_replaces_non_streaming_response_and_audits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_alter")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
            'permit(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-alter-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["tell me a secret"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", ["the secret is 42"])])

        await mw.process(ctx, call_next)
        assert isinstance(ctx.result, ChatResponse)
        assert ctx.result.text == "[REDACTED BY POLICY]"

    async def test_post_call_alter_unregistered_transform_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_alter_unknown")\n@stage("post")\n@action("alter")\n'
            '@alter_with("nonexistent_transform")\n'
            'permit(principal, action == Action::"model_call", resource);',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-alter-unknown-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", ["hi there"])])

        from parapetai_agent.maf import GovernanceDenied

        with pytest.raises(GovernanceDenied):
            await mw.process(ctx, call_next)

    async def test_post_call_streaming_registers_audit_only_hook_never_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A streamed response can't be blocked or rewritten -- chunks
        already reached the caller by the time a finalized-response hook
        can fire (see ParapetChatMiddleware's class docstring). process()
        itself must never raise for a streaming call no matter what a
        post-call rule would have decided; it can only register a hook
        that audits once MAF finalizes the stream."""
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_deny_stream")\n@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-stream-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
            stream=True,
        )
        assert ctx.stream_result_hooks == []

        await mw.process(ctx, _noop_call_next)  # must not raise even though stream=True

        assert len(ctx.stream_result_hooks) == 1

    async def test_post_call_streaming_hook_audits_would_deny_without_blocking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Directly invokes the real registered closure (not a
        reimplementation) with a synthetic finalized response, the same
        shape MAF's ResponseStream would call it with once the caller has
        consumed every chunk -- proves it audits the would-be deny and
        returns the response UNCHANGED, never blocking anything at that
        point (there's nothing left to block)."""
        from structlog.testing import capture_logs

        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "gpt-4o-mini")
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("post_deny_stream")\n@stage("post")\n'
            'forbid(principal, action == Action::"model_call", resource)\n'
            "when { context has response_preview && "
            'context.response_preview like "*SSN*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="post-stream-audit-test", tenant="default")
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
            stream=True,
        )
        await mw.process(ctx, _noop_call_next)
        registered_hook = ctx.stream_result_hooks[0]

        finalized = ChatResponse(messages=[Message("assistant", ["your SSN is 123-45-6789"])])
        with capture_logs() as events:
            returned = await registered_hook(finalized)

        assert returned is finalized  # unmodified
        assert any(e.get("event") == "post_call_would_deny_streaming" for e in events)


class TestToolPostCallGovernance:
    async def test_post_call_deny_substitutes_result(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("tool_post_deny")\n@stage("post")\n'
            'forbid(principal, action == Action::"tool_call", resource)\n'
            "when { context has tool_result_preview && "
            'context.tool_result_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-post-deny-test", tenant="default")
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_secret", func=lambda: "the secret is 42")
        ctx = FunctionInvocationContext(function=fn, arguments={})

        async def call_next() -> None:
            ctx.result = "the secret is 42"

        await mw.process(ctx, call_next)
        assert "GOVERNANCE_DENIED" in str(ctx.result)

    async def test_post_call_allows_result_that_does_not_match_any_rule(
        self, tmp_path: Path
    ) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("tool_post_deny")\n@stage("post")\n'
            'forbid(principal, action == Action::"tool_call", resource)\n'
            "when { context has tool_result_preview && "
            'context.tool_result_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-post-allow-test", tenant="default")
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_status", func=lambda: "status: ok")
        ctx = FunctionInvocationContext(function=fn, arguments={})

        async def call_next() -> None:
            ctx.result = "status: ok"

        await mw.process(ctx, call_next)
        assert ctx.result == "status: ok"  # untouched

    async def test_post_call_alter_replaces_result_and_audits(self, tmp_path: Path) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("tool_post_alter")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
            'permit(principal, action == Action::"tool_call", resource)\n'
            "when { context has tool_result_preview && "
            'context.tool_result_preview like "*secret*" };',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-post-alter-test", tenant="default")
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_secret", func=lambda: "the secret is 42")
        ctx = FunctionInvocationContext(function=fn, arguments={})

        async def call_next() -> None:
            ctx.result = "the secret is 42"

        await mw.process(ctx, call_next)
        assert ctx.result == "[REDACTED BY POLICY]"

    async def test_post_call_alter_unregistered_transform_fails_closed(
        self, tmp_path: Path
    ) -> None:
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("tool_post_alter_unknown")\n@stage("post")\n@action("alter")\n'
            '@alter_with("nonexistent_transform")\n'
            'permit(principal, action == Action::"tool_call", resource);',
        )
        engine = PolicyEngine(policy_dir)
        caller = Caller(agent_id="tool-post-alter-unknown-test", tenant="default")
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="anything", func=lambda: "some value")
        ctx = FunctionInvocationContext(function=fn, arguments={})

        async def call_next() -> None:
            ctx.result = "some value"

        await mw.process(ctx, call_next)
        assert "GOVERNANCE_DENIED" in str(ctx.result)

    async def test_build_middleware_alter_transforms_override_default(self, tmp_path: Path) -> None:
        """Confirms alter_transforms= actually threads from
        build_middleware() through to the constructed middleware, and that
        a caller-supplied name OVERRIDES (not merely supplements) the
        built-in DEFAULT_ALTER_TRANSFORMS entry for the same name."""
        policy_dir = _custom_policy_dir(
            tmp_path,
            '@id("custom_alter")\n@stage("post")\n@action("alter")\n@alter_with("redact_all")\n'
            'permit(principal, action == Action::"tool_call", resource);',
        )

        def my_redact(_value: object) -> object:
            return "custom replacement"

        chat_mw, func_mw = build_middleware(
            policy_dir,
            agent_id="alter-transforms-override-test",
            alter_transforms={"redact_all": my_redact},
        )
        fn = FunctionTool(name="anything", func=lambda: "original")
        ctx = FunctionInvocationContext(function=fn, arguments={})

        async def call_next() -> None:
            ctx.result = "original"

        await func_mw.process(ctx, call_next)
        assert ctx.result == "custom replacement"


# ── identity claims passthrough ─────────────────────────────────────────────


class TestIdentityClaims:
    def test_extracts_claims_dict(self) -> None:
        claims = _identity_claims({"identity_claims": {"oid": "abc-123", "tid": "tenant-1"}})
        assert claims == {"oid": "abc-123", "tid": "tenant-1"}

    def test_missing_claims_is_empty_not_an_error(self) -> None:
        assert _identity_claims(None) == {}
        assert _identity_claims({}) == {}
        assert _identity_claims({"other_key": "value"}) == {}

    def test_non_dict_claims_ignored_not_coerced(self) -> None:
        assert _identity_claims({"identity_claims": "not-a-dict"}) == {}

    async def test_claims_flow_into_snapshot_context_for_cedar(self) -> None:
        """The actual point: identity_claims must land in the dict that goes
        to PolicyEngine.evaluate(), so a Cedar rule could reference
        context.identity_claims.oid."""
        engine, caller = _engine_and_caller()
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_order", func=lambda order_id: f"order {order_id}")
        ctx = FunctionInvocationContext(
            function=fn,
            arguments={"order_id": "12345"},
            kwargs={"identity_claims": {"oid": "user-42"}},
        )

        await mw.process(ctx, _noop_call_next)
        # Reaching here without a policy error confirms identity_claims
        # round-tripped through Snapshot.to_context() -> cedarpy without
        # raising -- a malformed context value would fail closed with an
        # evaluation error (Decision.errors), which the deny path doesn't hit.

    def test_extracts_roles_list(self) -> None:
        assert _identity_roles({"identity_roles": ["OrderViewer", "Admin"]}) == [
            "OrderViewer",
            "Admin",
        ]

    def test_missing_roles_is_empty_not_an_error(self) -> None:
        assert _identity_roles(None) == []
        assert _identity_roles({}) == []
        assert _identity_roles({"identity_claims": {"oid": "x"}}) == []

    def test_non_list_roles_ignored_not_coerced(self) -> None:
        assert _identity_roles({"identity_roles": "OrderViewer"}) == []

    async def test_chat_middleware_reads_function_invocation_kwargs_not_kwargs(self) -> None:
        """Regression guard for a real bug: ChatContext.kwargs (populated
        from Agent.run(client_kwargs=...)) and
        ChatContext.function_invocation_kwargs (populated from
        Agent.run(function_invocation_kwargs=...)) are TWO SEPARATE
        attributes. identity_claims/identity_roles must be read from the
        latter -- reading .kwargs silently produced empty identity on every
        real Agent.run(), never caught before because the only prior test
        exercising this built ChatContext by hand with .kwargs set
        directly, sidestepping the real Agent.run() plumbing entirely."""
        monkeypatch_engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(monkeypatch_engine, caller)
        seen_contexts: list[dict[str, object]] = []
        real_evaluate = monkeypatch_engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            seen_contexts.append(dict(kwargs["context"]))  # type: ignore[arg-type]
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        monkeypatch_engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
            kwargs={"identity_claims": {"oid": "WRONG-should-not-appear"}},
            function_invocation_kwargs={
                "identity_claims": {"oid": "user-42"},
                "identity_roles": ["OrderViewer"],
            },
        )
        await mw.process(ctx, _noop_call_next)

        assert len(seen_contexts) == 1
        assert seen_contexts[0].get("identity_claims") == {"oid": "user-42"}
        assert seen_contexts[0].get("identity_roles") == ["OrderViewer"]

    async def test_live_role_gate_via_real_agent_run(self, fake_upstream: None) -> None:
        """The real path this backs: a caller does
        agent.run(..., function_invocation_kwargs={"identity_roles": [...]})
        and Cedar's real lookup_order_requires_order_viewer_role rule
        (policies/30-identity.cedar) decides based on it -- no directly
        constructed context, no monkeypatching of engine.evaluate, just
        Agent.run() driving the full real stack against real Cedar.
        fake_upstream is a module-scoped fixture defined below this class;
        pytest resolves it fine regardless of which class requests it."""
        called = {"with_role": False, "without_role": False}

        def lookup_order(order_id: str) -> str:
            called["with_role"] = True
            return f"order {order_id}: shipped"

        chat_mw, func_mw = build_middleware(POLICIES, POLICIES / "entities.json", "role-test")
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            await agent.run(
                "Look up order 12345.",
                function_invocation_kwargs={"identity_roles": ["OrderViewer"]},
            )
        assert called["with_role"] is True

        def lookup_order_denied(order_id: str) -> str:
            called["without_role"] = True
            return f"order {order_id}: shipped"

        chat_mw2, func_mw2 = build_middleware(
            POLICIES, POLICIES / "entities.json", "role-test-denied"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order_denied],
            middleware=[chat_mw2, func_mw2],
        ) as agent:
            await agent.run(
                "Look up order 12345.",
                function_invocation_kwargs={"identity_roles": ["SomeOtherRole"]},
            )
        assert called["without_role"] is False


# ── live end-to-end: real Agent.run(), real fake upstream, real MCP server ─


@pytest.fixture(scope="module")
def fake_upstream() -> None:
    import subprocess
    import time

    import httpx

    proc = subprocess.Popen(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [
            shutil.which("uv"),
            "run",
            "--no-project",
            "--with",
            "fastapi",
            "--with",
            "uvicorn",
            "python3",
            str(REPO_ROOT / "conformance" / "fake-upstream" / "app.py"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            httpx.get("http://127.0.0.1:9001/v1/chat/completions", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("fake upstream did not start (uv or network unavailable)")
    yield
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def mcp_server() -> None:
    import subprocess
    import time

    import httpx

    proc = subprocess.Popen(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [
            shutil.which("uv"),
            "run",
            "--no-project",
            "--with",
            "mcp==1.29.0",
            "python3",
            str(REPO_ROOT / "conformance" / "mcp-probe" / "server.py"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            httpx.get("http://127.0.0.1:8765/mcp", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("mcp server did not start (uv or network unavailable)")
    yield
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def mcp_websocket_server() -> None:
    """Backs spike/maf_mcp_check/websocket_server.py -- see that file's
    docstring for why it can't just be mcp.run(transport=...) like
    stdio_server.py, and why this transport is deprecated upstream."""
    import subprocess
    import time

    import httpx

    proc = subprocess.Popen(  # noqa: S603 -- fixed, hardcoded argv, not untrusted input
        [
            shutil.which("uv"),
            "run",
            "--no-project",
            "--with",
            "mcp==1.29.0",
            "--with",
            "uvicorn",
            "python3",
            str(REPO_ROOT / "spike" / "maf_mcp_check" / "websocket_server.py"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            # A plain GET can't complete the websocket handshake, but a fast
            # rejection (not a connection error) proves the ASGI server is up.
            httpx.get("http://127.0.0.1:8766/mcp", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("mcp websocket server did not start (harness unavailable)")
    yield
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9001/v1")
    monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "fake-model")


class TestToolSourcesLiveEndToEnd:
    async def test_native_tool_allowed(self, fake_upstream: None) -> None:
        called = {"ran": False}

        def get_server_status() -> str:
            called["ran"] = True
            return "status: ok"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "native-allow-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[get_server_status],
            middleware=[chat_mw, func_mw],
        ) as agent:
            result = await agent.run("What is the server status?")
            assert result.text
        assert called["ran"] is True

    async def test_native_tool_denied_by_name(self, fake_upstream: None) -> None:
        """execute_shell is in policies/20-tools.cedar's tool_destructive_denied
        list -- a real policy, not a test-only rule."""
        called = {"ran": False}

        def execute_shell(command: str) -> str:
            called["ran"] = True
            return "should never run"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "native-deny-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[execute_shell],
            middleware=[chat_mw, func_mw],
        ) as agent:
            await agent.run("Run a shell command.")
        assert called["ran"] is False

    async def test_mcp_streamable_http_tool_allowed(
        self, fake_upstream: None, mcp_server: None
    ) -> None:
        from agent_framework import MCPStreamableHTTPTool

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "mcp-http-allow-test"
        )
        async with (
            MCPStreamableHTTPTool(name="probe", url="http://127.0.0.1:8765/mcp") as mcp_tool,
            Agent(
                client=OpenAIChatCompletionClient(),
                name="A",
                instructions="Use the tool.",
                tools=mcp_tool,
                middleware=[chat_mw, func_mw],
            ) as agent,
        ):
            result = await agent.run("Look up order 12345.")
            assert result.text

    async def test_mcp_streamable_http_tool_denied(self, mcp_server: None) -> None:
        """Drives ParapetFunctionMiddleware directly against the *real*
        MCP-sourced FunctionTool for execute_shell (loaded from the live MCP
        server, not synthetic) -- fake-upstream always picks whichever tool
        is declared first in a request, and lookup_order is first in
        server.py's declaration order, so a full Agent.run() conversation
        can't be made to choose execute_shell without editing that shared
        fixture. This still exercises the identical code path a real
        model-chosen execute_shell call would hit."""
        from agent_framework import MCPStreamableHTTPTool

        engine, caller = _engine_and_caller()
        func_mw = ParapetFunctionMiddleware(engine, caller)

        async with MCPStreamableHTTPTool(name="probe", url="http://127.0.0.1:8765/mcp") as mcp_tool:
            execute_shell = next(f for f in mcp_tool.functions if f.name == "execute_shell")
            ctx = FunctionInvocationContext(
                function=execute_shell, arguments={"command": "echo hi"}
            )
            called = {"next": False}

            async def call_next() -> None:
                called["next"] = True

            await func_mw.process(ctx, call_next)
            assert called["next"] is False
            assert ctx.result is not None and "GOVERNANCE_DENIED" in str(ctx.result)

    async def test_mcp_websocket_tool_allowed(
        self, fake_upstream: None, mcp_websocket_server: None
    ) -> None:
        """mcp.server.websocket / mcp.client.websocket are DEPRECATED as of
        mcp==1.29.0 (this repo's pinned version), slated for removal in mcp
        2.0 -- "WebSocket was never part of the MCP specification." This
        exists to prove enforcement parity for anyone currently stuck on it,
        not as an endorsement to build new integrations against it (prefer
        MCPStreamableHTTPTool, per that deprecation notice, for anything
        new). filterwarnings here because MCPWebsocketTool itself triggers
        the deprecation warning just by importing/constructing it, which is
        expected noise for this specific test, not a bug to fix."""
        import warnings

        from agent_framework import MCPWebsocketTool

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "mcp-websocket-allow-test"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            async with (
                MCPWebsocketTool(name="probe-ws", url="ws://127.0.0.1:8766/mcp") as mcp_tool,
                Agent(
                    client=OpenAIChatCompletionClient(),
                    name="A",
                    instructions="Use the tool.",
                    tools=mcp_tool,
                    middleware=[chat_mw, func_mw],
                ) as agent,
            ):
                result = await agent.run("Look up order 12345.")
                assert result.text

    async def test_mcp_websocket_tool_denied(self, mcp_websocket_server: None) -> None:
        """Same rationale as test_mcp_streamable_http_tool_denied/
        test_mcp_stdio_tool_denied -- direct FunctionInvocationContext
        against the real websocket-sourced FunctionTool, proving parity:
        the denial path is identical code regardless of transport."""
        import warnings

        from agent_framework import MCPWebsocketTool

        engine, caller = _engine_and_caller()
        func_mw = ParapetFunctionMiddleware(engine, caller)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            async with MCPWebsocketTool(name="probe-ws", url="ws://127.0.0.1:8766/mcp") as mcp_tool:
                execute_shell = next(f for f in mcp_tool.functions if f.name == "execute_shell")
                ctx = FunctionInvocationContext(
                    function=execute_shell, arguments={"command": "echo hi"}
                )
                called = {"next": False}

                async def call_next() -> None:
                    called["next"] = True

                await func_mw.process(ctx, call_next)
                assert called["next"] is False
                assert ctx.result is not None and "GOVERNANCE_DENIED" in str(ctx.result)

    async def test_mcp_stdio_tool_allowed(self, fake_upstream: None) -> None:
        from agent_framework import MCPStdioTool

        stdio_server = str(REPO_ROOT / "conformance" / "mcp-probe" / "stdio_server.py")
        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "mcp-stdio-allow-test"
        )
        async with (
            MCPStdioTool(
                name="probe-stdio",
                command=shutil.which("uv"),
                args=["run", "--no-project", "--with", "mcp==1.29.0", "python3", stdio_server],
            ) as mcp_tool,
            Agent(
                client=OpenAIChatCompletionClient(),
                name="A",
                instructions="Use the tool.",
                tools=mcp_tool,
                middleware=[chat_mw, func_mw],
            ) as agent,
        ):
            result = await agent.run("Look up order 12345.")
            assert result.text

    async def test_mcp_stdio_tool_denied(self) -> None:
        """Same rationale as test_mcp_streamable_http_tool_denied -- direct
        FunctionInvocationContext against the real stdio-sourced FunctionTool,
        proving parity: the denial path is identical code regardless of
        transport, not just the allow path."""
        from agent_framework import MCPStdioTool

        stdio_server = str(REPO_ROOT / "conformance" / "mcp-probe" / "stdio_server.py")
        engine, caller = _engine_and_caller()
        func_mw = ParapetFunctionMiddleware(engine, caller)

        async with MCPStdioTool(
            name="probe-stdio",
            command=shutil.which("uv"),
            args=["run", "--no-project", "--with", "mcp==1.29.0", "python3", stdio_server],
        ) as mcp_tool:
            execute_shell = next(f for f in mcp_tool.functions if f.name == "execute_shell")
            ctx = FunctionInvocationContext(
                function=execute_shell, arguments={"command": "echo hi"}
            )
            called = {"next": False}

            async def call_next() -> None:
                called["next"] = True

            await func_mw.process(ctx, call_next)
            assert called["next"] is False
            assert ctx.result is not None and "GOVERNANCE_DENIED" in str(ctx.result)


class TestPostCallRegressionWithRealBundle:
    async def test_native_tool_allowed_with_pre_and_post_both_now_evaluated(
        self, fake_upstream: None
    ) -> None:
        """policies/ (the real, shared bundle) has no @stage/@action
        annotations anywhere -- both the new pre and post evaluations run
        against the identical, unfiltered rule set, so a real end-to-end
        run must behave exactly like it did before post-call evaluation
        existed. Regression guard for the two new decisions added per
        model/tool call, not just a synthetic-context check."""
        called = {"ran": False}

        def get_server_status() -> str:
            called["ran"] = True
            return "status: ok"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "post-regression-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[get_server_status],
            middleware=[chat_mw, func_mw],
        ) as agent:
            result = await agent.run("What is the server status?")
            assert result.text
        assert called["ran"] is True


# ── OpenTelemetry: log_mode (streaming/buffered) and exit-flush ────────────


class TestOtelLogMode:
    """configure_otel()'s log_mode= -- Simple (streaming) vs Batch
    (buffered) processors for the two EXTERNAL exporters (azure_monitor,
    otlp), and flush_otel()'s shutdown-on-exit behavior. Inspects
    maf_module._otel_tracer_provider/_otel_logger_provider directly
    (module state configure_otel() always sets, unconditionally) rather
    than trace.get_tracer_provider() -- OTel's own global registration
    is a set-once-per-process guard in some SDK versions, which would
    make later calls in this same test session silently no-op there;
    reading our own module state sidesteps that entirely.

    Real bug, found live building this: configure_otel() really does call
    trace.set_tracer_provider() -- a genuine PROCESS-WIDE, normally
    set-ONCE registration, not scoped to this test class at all. This
    module's own module-level `_tracer = trace.get_tracer(__name__)` is a
    lazy proxy that resolves against WHATEVER is globally registered at
    call time (see this file's own module docstring on _tracer), so
    leaving a real provider registered here broke TestOtelCorrelation's
    OWN tests (which expect the untouched no-op default) the moment they
    ran afterward in the SAME pytest session -- observed as a real
    failure there, immediately followed by the NEXT test hanging
    (a stopped BatchSpanProcessor worker thread's queue never draining).
    conftest.py's own _reset_otel_module_state autouse fixture undoes
    that registration after EVERY test in this whole test package now
    (not just this class) -- build_middleware() itself calls
    configure_otel() whenever a control plane is configured, so this
    leak risk is no longer confined to tests that call configure_otel()
    directly; see that fixture's own docstring."""

    def test_streaming_uses_simple_processors(self) -> None:
        import parapetai_agent.maf as maf_module

        maf_module.configure_otel(
            service_name="test",
            otlp_endpoint="https://cp.example",
            console=False,
            log_mode="streaming",
        )
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = [type(p).__name__ for p in tp._active_span_processor._span_processors]
        assert processor_types == ["SimpleSpanProcessor"]

    def test_buffered_is_the_default_and_uses_batch_processors(self) -> None:
        import parapetai_agent.maf as maf_module

        maf_module.configure_otel(
            service_name="test", otlp_endpoint="https://cp.example", console=False
        )
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = [type(p).__name__ for p in tp._active_span_processor._span_processors]
        assert processor_types == ["BatchSpanProcessor"]

    def test_batch_schedule_delay_defaults_to_two_minutes(self) -> None:
        from parapetai_agent.maf import _DEFAULT_BATCH_SCHEDULE_DELAY_S

        assert _DEFAULT_BATCH_SCHEDULE_DELAY_S == 120.0

    def test_console_processor_is_always_simple_regardless_of_log_mode(self) -> None:
        """console=True's own processor is unaffected by log_mode -- it's
        for live debugging, never worth batching."""
        import parapetai_agent.maf as maf_module

        maf_module.configure_otel(service_name="test", console=True, log_mode="buffered")
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = [type(p).__name__ for p in tp._active_span_processor._span_processors]
        assert processor_types == ["SimpleSpanProcessor"]

    def test_flush_otel_is_a_safe_noop_before_configure_otel_runs(self) -> None:
        from parapetai_agent.maf import flush_otel

        flush_otel()  # must not raise

    def test_flush_otel_shuts_down_and_is_idempotent(self) -> None:
        import parapetai_agent.maf as maf_module
        from parapetai_agent.maf import flush_otel

        maf_module.configure_otel(
            service_name="test", otlp_endpoint="https://cp.example", console=False
        )
        flush_otel()
        flush_otel()  # a second call must not raise either


# ── OpenTelemetry: real trace correlation and LogRecord shape ──────────────


class TestOtelCorrelation:
    async def test_trace_and_log_correlation_via_real_agent_run(
        self, fake_upstream: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real Agent.run(), real Cedar decisions, real OTel SDK (not a
        directly-constructed context) -- verifies:

        1. parapetai.tool_call's span is a genuine CHILD of the parapetai.model_call
           span that triggered it (same trace_id, tool_call.parent.span_id
           == model_call's span_id) -- proving the explicit
           _parent_context_from_correlation() linkage actually works end to
           end. This is NOT ambient nesting: ChatMiddleware and
           FunctionMiddleware fire as sequential siblings from MAF's own
           run loop (verified earlier, not assumed), so if the explicit
           linkage were broken, tool_call would show up as its OWN
           trace_id with no parent -- which is exactly the failure mode
           this test would catch.
        2. The "decision" LogRecord's trace_id/span_id match the span that
           was active when _audit() ran -- proving Logger.emit()'s
           automatic ambient-context pickup (verified directly against
           opentelemetry-sdk's source) actually fires here, not just in
           isolation.
        3. identity_claims.oid/identity_roles land on the LogRecord as the
           enduser.id/user.roles semantic-convention attributes
           specifically, not just buried in a nested dict.
        """
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        import parapetai_agent.maf as maf_module

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        otel_trace.set_tracer_provider(tracer_provider)
        # maf_module._tracer was created at import time, before any provider
        # existed -- verified separately that trace.get_tracer() returns a
        # lazy proxy that still picks up a provider set later, so no
        # monkeypatch is needed for _tracer specifically, only for
        # _otel_logger (a plain None-checked reference, not a lazy proxy).

        log_exporter = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        monkeypatch.setattr(maf_module, "_otel_logger", logger_provider.get_logger("test"))

        def lookup_order(order_id: str) -> str:
            return f"order {order_id}: shipped"

        chat_mw, func_mw = build_middleware(POLICIES, POLICIES / "entities.json", "otel-test")
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            await agent.run(
                "Look up order 12345.",
                function_invocation_kwargs={
                    "identity_claims": {"oid": "user-otel-test"},
                    "identity_roles": ["OrderViewer"],
                },
            )

        # Two parapetai.model_call spans exist per run (one before the tool
        # call declares the tool, one after carries the tool result back)
        # -- a name-keyed dict would silently collapse them to whichever
        # ran last, which is NOT necessarily the one that triggered the
        # tool call. Keep them as a list and match by span_id instead.
        all_spans = span_exporter.get_finished_spans()
        model_spans = [s for s in all_spans if s.name == "parapetai.model_call"]
        tool_spans = [s for s in all_spans if s.name == "parapetai.tool_call"]
        assert len(model_spans) == 2
        assert len(tool_spans) == 1
        tool_span = tool_spans[0]
        assert tool_span.parent is not None
        triggering_model_span = next(
            (s for s in model_spans if s.context.span_id == tool_span.parent.span_id), None
        )
        assert triggering_model_span is not None, (
            "tool_call span's parent must be one of the two model_call spans"
        )
        assert tool_span.context.trace_id == triggering_model_span.context.trace_id

        tool_call_logs = [
            log_data.log_record
            for log_data in log_exporter.get_finished_logs()
            if (log_data.log_record.attributes or {}).get("action") == "tool_call"
        ]
        # TWO decisions per tool call now, not one: ParapetFunctionMiddleware
        # evaluates pre- AND post-call (see its class docstring) within the
        # SAME parapetai.tool_call span -- both log records must attribute to
        # that one span, not two different ones.
        assert len(tool_call_logs) == 2
        for record in tool_call_logs:
            assert record.trace_id == tool_span.context.trace_id
            assert record.span_id == tool_span.context.span_id
            assert record.attributes is not None
            assert record.attributes["enduser.id"] == "user-otel-test"
            # OTel's attribute storage coerces list values to tuples
            # internally (verified: this is what actually comes back, not a
            # list) -- list(...) here, not a literal tuple, to keep the
            # assertion reading as "same contents" rather than assuming the
            # SDK's specific internal representation.
            assert list(record.attributes["user.roles"]) == ["OrderViewer"]


class TestOtelOpenInferenceAttributes:
    """OpenInference span attributes (docs/adr/0007) -- metadata attrs
    (span kind, model name, token counts, tool name) ship unconditionally;
    content_bearing attrs (llm.input_messages/output_messages,
    tool.parameters) only with PARAPETAI_OTEL_LOG_CONTENT=true. Synthetic
    ChatContext/FunctionInvocationContext + a real TracerProvider/
    InMemorySpanExporter, same strategy TestOtelCorrelation uses for real
    span capture, without needing fake_upstream's live subprocess."""

    @staticmethod
    def _exporter() -> Any:
        """conftest.py's autouse _reset_otel_module_state resets the
        GLOBAL trace._TRACER_PROVIDER after every test, so
        set_tracer_provider() below succeeds fresh each time (each test
        is effectively "the first" as far as that global goes). But
        maf.py's module-level `_tracer = trace.get_tracer(__name__)` is a
        ProxyTracer, and ProxyTracer CACHES its resolved real tracer on
        first use (`if self._real_tracer: return self._real_tracer` --
        verified against opentelemetry.trace.ProxyTracer source) and
        never re-checks afterward. Left alone, only the FIRST test in
        this class that exercises any span would ever populate its own
        exporter -- every later test's spans would keep flowing to the
        first test's now-orphaned provider instead. Clearing the cache
        here forces a fresh resolution against the provider just set."""
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        import parapetai_agent.maf as maf_module

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        otel_trace.set_tracer_provider(tracer_provider)
        maf_module._tracer._real_tracer = None
        return span_exporter

    async def test_model_call_metadata_attrs_present_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.delenv("PARAPETAI_OTEL_LOG_CONTENT", raising=False)
        span_exporter = self._exporter()
        engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["hello"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(
                messages=[Message("assistant", ["hi there"])],
                usage_details={
                    "input_token_count": 12,
                    "output_token_count": 4,
                    "total_token_count": 16,
                },
            )

        await mw.process(ctx, call_next)

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.model_call"
        )
        attrs = span.attributes or {}
        assert attrs["openinference.span.kind"] == "LLM"
        assert attrs["llm.model_name"] == "gpt-4o-mini"
        assert attrs["llm.token_count.prompt"] == 12
        assert attrs["llm.token_count.completion"] == 4
        assert attrs["llm.token_count.total"] == 16
        assert "llm.input_messages" not in attrs
        assert "llm.output_messages" not in attrs

    async def test_model_call_content_attrs_present_with_env_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setenv("PARAPETAI_OTEL_LOG_CONTENT", "true")
        span_exporter = self._exporter()
        engine, caller = _engine_and_caller()
        mw = ParapetChatMiddleware(engine, caller)
        ctx = ChatContext(
            client=OpenAIChatCompletionClient(),
            messages=[Message("user", ["what is the secret plan"])],
            options={"model": "gpt-4o-mini", "tools": []},
        )

        async def call_next() -> None:
            ctx.result = ChatResponse(messages=[Message("assistant", ["the plan is X"])])

        await mw.process(ctx, call_next)

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.model_call"
        )
        attrs = span.attributes or {}
        assert list(attrs["llm.input_messages"]) == ["what is the secret plan"]
        assert attrs["llm.output_messages"] == "the plan is X"

    async def test_tool_call_metadata_attrs_present_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PARAPETAI_OTEL_LOG_CONTENT", raising=False)
        span_exporter = self._exporter()
        engine, caller = _engine_and_caller()
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_status", func=lambda: "status: ok")
        ctx = FunctionInvocationContext(function=fn, arguments={"order_id": "12345"})

        async def call_next() -> None:
            ctx.result = "status: ok"

        await mw.process(ctx, call_next)

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.tool_call"
        )
        attrs = span.attributes or {}
        assert attrs["openinference.span.kind"] == "TOOL"
        assert attrs["tool.name"] == "lookup_status"
        assert "tool.parameters" not in attrs
        assert "output.value" not in attrs

    async def test_tool_call_content_attrs_present_with_env_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARAPETAI_OTEL_LOG_CONTENT", "true")
        span_exporter = self._exporter()
        engine, caller = _engine_and_caller()
        mw = ParapetFunctionMiddleware(engine, caller)
        fn = FunctionTool(name="lookup_status", func=lambda: "status: ok")
        ctx = FunctionInvocationContext(function=fn, arguments={"order_id": "12345"})

        async def call_next() -> None:
            ctx.result = "status: ok"

        await mw.process(ctx, call_next)

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "parapetai.tool_call"
        )
        attrs = span.attributes or {}
        assert "order_id" in attrs["tool.parameters"]
        assert "12345" in attrs["tool.parameters"]
        assert attrs["output.value"] == "status: ok"


# ── GovernedAgent: automatic wiring, no middleware= needed at the call site ─


class TestGovernedAgent:
    async def test_enforces_without_explicit_middleware_kwarg(self, fake_upstream: None) -> None:
        """The actual point: no middleware=[...] passed here at all --
        GovernedAgent must construct and attach it internally. execute_shell
        is real, pre-existing policy (tool_destructive_denied), not a
        test-only rule."""
        called = {"ran": False}

        def execute_shell(command: str) -> str:
            called["ran"] = True
            return "should never run"

        async with GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[execute_shell],
            policy_dir=POLICIES,
            entities_path=POLICIES / "entities.json",
            agent_id="governed-agent-deny-test",
        ) as agent:
            await agent.run("Run a shell command.")
        assert called["ran"] is False

    async def test_allows_a_permitted_call_without_explicit_middleware_kwarg(
        self, fake_upstream: None
    ) -> None:
        called = {"ran": False}

        def lookup_order(order_id: str) -> str:
            called["ran"] = True
            return f"order {order_id}: shipped"

        async with GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            policy_dir=POLICIES,
            entities_path=POLICIES / "entities.json",
            agent_id="governed-agent-allow-test",
        ) as agent:
            await agent.run("Look up order 12345.")
        assert called["ran"] is True

    async def test_explicit_middleware_kwarg_still_runs_alongside_governance(
        self, fake_upstream: None
    ) -> None:
        """A caller's own middleware=[...] must not be silently dropped --
        GovernedAgent prepends governance, it doesn't replace what was
        passed."""
        from agent_framework import FunctionMiddleware

        seen_tool_calls: list[str] = []

        class RecordingMiddleware(FunctionMiddleware):  # type: ignore[misc]
            async def process(self, context, call_next):  # type: ignore[no-untyped-def]
                seen_tool_calls.append(context.function.name)
                await call_next()

        def lookup_order(order_id: str) -> str:
            return f"order {order_id}: shipped"

        async with GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[RecordingMiddleware()],
            policy_dir=POLICIES,
            entities_path=POLICIES / "entities.json",
            agent_id="governed-agent-extra-middleware-test",
        ) as agent:
            await agent.run("Look up order 12345.")
        assert seen_tool_calls == ["lookup_order"]


# ── Ambient identity: set once, no function_invocation_kwargs per call ──────


class TestAmbientIdentity:
    async def test_ambient_identity_drives_role_gate_without_kwargs(
        self, fake_upstream: None
    ) -> None:
        """No function_invocation_kwargs anywhere in this test -- identity
        flows purely from current_identity(), proving the ambient path
        alone is sufficient to drive a real Cedar decision
        (policies/30-identity.cedar's real role-gate rule)."""
        called = {"ran": False}

        def lookup_order(order_id: str) -> str:
            called["ran"] = True
            return f"order {order_id}: shipped"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "ambient-identity-allow-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            with current_identity(claims={"oid": "bob"}, roles=["OrderViewer"]):
                await agent.run("Look up order 12345.")
        assert called["ran"] is True

    async def test_ambient_identity_without_role_is_denied(self, fake_upstream: None) -> None:
        called = {"ran": False}

        def lookup_order(order_id: str) -> str:
            called["ran"] = True
            return f"order {order_id}: shipped"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "ambient-identity-deny-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            with current_identity(claims={"oid": "eve"}, roles=["SomeOtherRole"]):
                await agent.run("Look up order 12345.")
        assert called["ran"] is False

    async def test_explicit_kwargs_override_ambient_identity(self, fake_upstream: None) -> None:
        """Ambient says no role (would deny); explicit
        function_invocation_kwargs says OrderViewer (should allow) --
        explicit must win, matching _identity_claims/_identity_roles'
        documented precedence."""
        called = {"ran": False}

        def lookup_order(order_id: str) -> str:
            called["ran"] = True
            return f"order {order_id}: shipped"

        chat_mw, func_mw = build_middleware(
            POLICIES, POLICIES / "entities.json", "explicit-overrides-ambient-test"
        )
        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            with current_identity(claims={"oid": "eve"}, roles=["SomeOtherRole"]):
                await agent.run(
                    "Look up order 12345.",
                    function_invocation_kwargs={
                        "identity_claims": {"oid": "bob"},
                        "identity_roles": ["OrderViewer"],
                    },
                )
        assert called["ran"] is True

    async def test_ambient_identity_isolated_across_concurrent_tasks(
        self, fake_upstream: None
    ) -> None:
        """Real concurrency, not sequential awaits: two agent.run() calls
        running via asyncio.gather(), each inside its OWN
        current_identity() block with a DIFFERENT role, on separate Agent
        instances. Proves the ContextVar backing this doesn't leak one
        task's identity into the other's Cedar decision -- the same
        isolation guarantee _current_chat already relies on (see its
        docstring), verified here for _current_identity specifically."""
        called = {"with_role": False, "without_role": False}

        # Both named literally "lookup_order" (not e.g. "lookup_order_with_role")
        # -- conformance/fake-upstream's _DEFAULT_TOOL_ARGS only has a real
        # entry for that exact name; anything else gets empty {} args and
        # fails schema validation before ever reaching governance, which
        # looks identical to a deny without an audit trail to tell them
        # apart (the exact confusion documented in that file's own
        # comment). Distinct closures are enough to keep the two `called`
        # flags separate despite the shared name.
        def lookup_order_with_role(order_id: str) -> str:
            called["with_role"] = True
            return f"order {order_id}: shipped"

        lookup_order_with_role.__name__ = "lookup_order"

        def lookup_order_without_role(order_id: str) -> str:
            called["without_role"] = True
            return f"order {order_id}: shipped"

        lookup_order_without_role.__name__ = "lookup_order"

        async def run_with_role() -> None:
            chat_mw, func_mw = build_middleware(
                POLICIES, POLICIES / "entities.json", "concurrent-with-role"
            )
            async with Agent(
                client=OpenAIChatCompletionClient(),
                name="A",
                instructions="Use the tool.",
                tools=[lookup_order_with_role],
                middleware=[chat_mw, func_mw],
            ) as agent:
                with current_identity(claims={"oid": "bob"}, roles=["OrderViewer"]):
                    await asyncio.sleep(0)  # yield control, encourage interleaving
                    await agent.run("Look up order 12345.")

        async def run_without_role() -> None:
            chat_mw, func_mw = build_middleware(
                POLICIES, POLICIES / "entities.json", "concurrent-without-role"
            )
            async with Agent(
                client=OpenAIChatCompletionClient(),
                name="A",
                instructions="Use the tool.",
                tools=[lookup_order_without_role],
                middleware=[chat_mw, func_mw],
            ) as agent:
                with current_identity(claims={"oid": "eve"}, roles=[]):
                    await agent.run("Look up order 12345.")

        await asyncio.gather(run_with_role(), run_without_role())
        assert called["with_role"] is True
        assert called["without_role"] is False


# ── Agent identity from a token: RFC 8693 act / azp / appid ────────────────


def _make_jwt(payload: dict) -> str:
    import base64
    import json

    def _b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.fakesignature"


class TestEffectivePrincipal:
    def test_falls_back_to_caller_principal_with_no_ambient_agent_identity(self) -> None:
        caller = Caller(agent_id="static-agent")
        assert _effective_principal(caller) == 'Agent::"static-agent"'

    def test_ambient_agent_identity_overrides_caller_principal(self) -> None:
        caller = Caller(agent_id="static-agent")
        with agent_identity(claims={"client_id": "sp-999"}):
            assert _effective_principal(caller) == 'Agent::"sp-999"'
        # Restored after the block exits.
        assert _effective_principal(caller) == 'Agent::"static-agent"'

    def test_build_middleware_defaults_agent_id_to_anonymous(self) -> None:
        _, func_mw = build_middleware(POLICIES, POLICIES / "entities.json")
        assert func_mw.caller.agent_id == ANONYMOUS


class TestTokenDrivenIdentity:
    async def test_identity_from_bearer_token_drives_role_gate_and_principal(
        self, fake_upstream: None
    ) -> None:
        """One synthetic JWT carries BOTH the end user's OrderViewer role
        (drives the real policies/30-identity.cedar role gate) and an RFC
        8693 act claim for the agent's own Service Principal identity
        (overrides the Caller's static agent_id for this decision) --
        proving identity_from_bearer_token() wires both through a real
        Agent.run(), not just in isolation."""
        called = {"ran": False}

        def lookup_order(order_id: str) -> str:
            called["ran"] = True
            return f"order {order_id}: shipped"

        token = _make_jwt(
            {
                "oid": "bob-oid",
                "preferred_username": "bob@contoso.com",
                "roles": ["OrderViewer"],
                "act": {"sub": "sp-client-id-abc"},
            }
        )

        engine, _ = _engine_and_caller()
        caller = Caller(agent_id="static-fallback-agent")
        chat_mw = ParapetChatMiddleware(engine, caller)
        func_mw = ParapetFunctionMiddleware(engine, caller)

        seen_principals: list[str] = []
        real_evaluate = engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            seen_principals.append(str(kwargs["principal"]))
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            with identity_from_bearer_token(token):
                await agent.run("Look up order 12345.")

        assert called["ran"] is True  # role gate allowed it
        assert seen_principals, "expected at least one Cedar evaluation"
        assert all(p == 'Agent::"sp-client-id-abc"' for p in seen_principals), (
            f"expected every decision's principal to reflect the token's act claim, "
            f"got {seen_principals}"
        )

    async def test_identity_from_bearer_token_without_delegation_keeps_static_principal(
        self, fake_upstream: None
    ) -> None:
        """A token with no act/azp/appid claim leaves the Caller's static
        agent_id as the principal -- agent identity from a token is
        optional, this proves the "optional" half actually holds."""
        token = _make_jwt({"oid": "bob-oid", "roles": ["OrderViewer"]})

        engine, _ = _engine_and_caller()
        caller = Caller(agent_id="static-fallback-agent")
        chat_mw = ParapetChatMiddleware(engine, caller)
        func_mw = ParapetFunctionMiddleware(engine, caller)

        seen_principals: list[str] = []
        real_evaluate = engine.evaluate

        def _spy_evaluate(**kwargs: object) -> object:
            seen_principals.append(str(kwargs["principal"]))
            return real_evaluate(**kwargs)  # type: ignore[arg-type]

        engine.evaluate = _spy_evaluate  # type: ignore[method-assign]

        def lookup_order(order_id: str) -> str:
            return f"order {order_id}: shipped"

        async with Agent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="Use the tool.",
            tools=[lookup_order],
            middleware=[chat_mw, func_mw],
        ) as agent:
            with identity_from_bearer_token(token):
                await agent.run("Look up order 12345.")

        assert seen_principals
        assert all(p == 'Agent::"static-fallback-agent"' for p in seen_principals)


class TestBuildMiddlewareControlPlanePull:
    """build_middleware()'s opt-in control-plane pull -- mirrors the fix
    parapetai_gateway.server.main needed: the fetch must happen BEFORE PolicyEngine is
    constructed, since PolicyEngine.__init__ raises immediately if
    policy_dir has no .cedar files yet. Root cause of a real bug: a rule
    created via the control plane's web UI never took effect because
    build_middleware() (what examples/maf_webapp/run_example.py
    actually uses) had no path to the control plane at all -- it only ever
    read the repo's local policies/ directory, completely disconnected
    from whatever an operator provisioned."""

    @respx.mock
    def test_pulls_bundle_before_constructing_policy_engine(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / "policies"  # deliberately empty -- would
        # otherwise make PolicyEngine.__init__ raise PolicyLoadError if the
        # pull didn't happen first.
        respx.get("https://cp.example/api/v1/bundle").mock(
            return_value=Response(
                200,
                json={
                    "agent_id": "pa-1",
                    "digest": "abc123",
                    "files": {
                        "00-base.cedar": "permit (principal, action, resource);",
                        "entities.json": "[]",
                    },
                },
            )
        )
        heartbeat_route = respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )

        chat_mw, _ = build_middleware(
            agent_id="test-agent",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            pep_key_path=tmp_path / "pep.key",
            persist_policy_dir=policy_dir,
        )

        assert (policy_dir / "00-base.cedar").exists()
        assert chat_mw.engine.status["policy_files"] == 1
        assert heartbeat_route.called

    @respx.mock
    def test_without_control_plane_args_makes_no_http_call(self) -> None:
        # No respx routes registered at all -- @respx.mock raises on any
        # unmocked request, so this proves the default (opt-in, unset)
        # path makes zero outbound calls, unchanged from before this
        # feature existed.
        _, func_mw = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="local-only")
        assert func_mw.caller.agent_id == "local-only"

    @respx.mock
    def test_persist_pep_key_false_never_touches_disk(self, tmp_path: Path) -> None:
        """The full end-to-end wiring for a serverless/read-only-container
        deployment: no pep_key_path given (there's nowhere durable to put
        one), persist_pep_key=False -- registers a real, fresh Ed25519
        key without ever writing it anywhere, even though tmp_path is
        available and would normally be a plausible place to look."""
        respx.get("https://cp.example/api/v1/bundle").mock(
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
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        keys_route = respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )

        build_middleware(
            agent_id="ephemeral-key-test",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            persist_pep_key=False,
        )

        assert keys_route.called
        # tmp_path stands in for "the only writable directory available"
        # -- nothing landed there, and nothing landed at the real default
        # (~/.parapetai/pep_ed25519.key) either, since that path was never
        # even computed (persist_pep_key=False short-circuits before
        # resolving one -- see build_middleware's own resolved_pep_key_path).
        assert not any(tmp_path.iterdir())

    @respx.mock
    def test_env_var_fallback_triggers_the_pull(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        policy_dir = tmp_path / "policies"
        monkeypatch.setenv("PARAPETAI_CONTROL_PLANE_URL", "https://cp.example")
        monkeypatch.setenv("PARAPETAI_AGENT_SECRET", "the-secret")
        respx.get("https://cp.example/api/v1/bundle").mock(
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
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )
        monkeypatch.setenv("PARAPETAI_PEP_KEY_PATH", str(tmp_path / "pep.key"))

        build_middleware(persist_policy_dir=policy_dir)

        assert (policy_dir / "00-base.cedar").exists()

    @respx.mock
    def test_governed_agent_threads_control_plane_args_through(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / "policies"
        respx.get("https://cp.example/api/v1/bundle").mock(
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
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )

        agent = GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="A",
            instructions="noop",
            agent_id="test-agent",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            pep_key_path=tmp_path / "pep.key",
            persist_policy_dir=policy_dir,
        )

        assert (policy_dir / "00-base.cedar").exists()
        assert agent.middleware[0].engine.status["policy_files"] == 1


class TestBundledDefaultPolicy:
    """policy_dir/entities_path are both optional now -- omitting them
    uses the policy set bundled inside this package (default_policies/),
    not a required setup step. See _resolve_policy_source()'s own
    docstring."""

    def test_omitting_policy_dir_enforces_the_bundled_default(self) -> None:
        chat_mw, _ = build_middleware(agent_id="bundled-default-test")
        # Base permits from default_policies/00-base.cedar -- confirms
        # this isn't an empty/no-op engine, it's a real, evaluated policy
        # set, same shape as this repo's own policies/00-base.cedar.
        decision = chat_mw.engine.evaluate(
            principal="bundled-default-test",
            action="model_call",
            resource="test",
            context={},
        )
        assert decision.allowed

    def test_bundled_default_dir_is_a_real_installed_path(self) -> None:
        from parapetai_agent.maf import _bundled_default_policy_dir

        default_dir = _bundled_default_policy_dir()
        assert (default_dir / "00-base.cedar").is_file()
        assert (default_dir / "entities.json").is_file()

    @respx.mock
    def test_control_plane_pull_without_persist_policy_dir_stays_in_memory(
        self, tmp_path: Path
    ) -> None:
        """The new default control-plane path: no persist_policy_dir ->
        the pulled bundle is applied to the engine's memory only
        (PolicyEngine.load_from_bundle(), via poll_once's
        persist_to_disk=False) -- nothing is ever written to disk."""
        respx.get("https://cp.example/api/v1/bundle").mock(
            return_value=Response(
                200,
                json={
                    "agent_id": "pa-1",
                    "digest": "real-bundle-digest",
                    "files": {
                        "00-base.cedar": (
                            'forbid (principal, action == Action::"model_call", resource);'
                        ),
                        "entities.json": "[]",
                    },
                },
            )
        )
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )
        # Nothing under tmp_path before the call, nothing after -- proves
        # this branch never touches disk, not just that we didn't pass a
        # path to check.
        assert not any(tmp_path.iterdir())

        chat_mw, _ = build_middleware(
            agent_id="memory-only-test",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            pep_key_path=tmp_path / "pep.key",
        )

        # The REAL pulled bundle (a forbid) took over in memory, not just
        # the bundled default (a permit) -- proves poll_once's in-memory
        # apply actually ran, not merely that construction succeeded.
        decision = chat_mw.engine.evaluate(
            principal="memory-only-test",
            action="model_call",
            resource="test",
            context={},
        )
        assert not decision.allowed
        # Only pep.key was ever written -- no policy directory appeared.
        written = {p.name for p in tmp_path.iterdir()}
        assert written == {"pep.key"}


class TestBuildMiddlewareIdentityRegistry:
    """build_middleware()'s per-identity reuse -- the actual fix for
    "why does every call duplicate setup and threads." Same resolved key
    (policy_dir, entities_path, agent_id, tenant, control_plane_url) must
    return the SAME objects (identity, not just equal) and do real
    construction exactly once; a different key must get its own,
    independent setup."""

    def test_same_key_returns_the_same_middleware_instances(self) -> None:
        chat_mw1, func_mw1 = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="a")
        chat_mw2, func_mw2 = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="a")

        assert chat_mw1 is chat_mw2
        assert func_mw1 is func_mw2
        assert chat_mw1.engine is chat_mw2.engine

    def test_different_agent_id_gets_independent_instances(self) -> None:
        chat_mw1, _ = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="a")
        chat_mw2, _ = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="b")

        assert chat_mw1 is not chat_mw2
        assert chat_mw1.caller.agent_id == "a"
        assert chat_mw2.caller.agent_id == "b"

    def test_reset_registry_forces_fresh_construction(self) -> None:
        chat_mw1, _ = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="a")
        reset_middleware_registry()
        chat_mw2, _ = build_middleware(POLICIES, POLICIES / "entities.json", agent_id="a")

        assert chat_mw1 is not chat_mw2

    @respx.mock
    def test_repeated_calls_with_control_plane_pull_only_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the background poller to a no-op. It's irrelevant to what this
        # test proves (the registry cache: 3 build_middleware calls do setup
        # ONCE), and its real form polls the bundle immediately on the daemon
        # thread it starts -- a second, non-deterministic bundle GET that
        # races this test's assertion (flaky "assert 2 == 1" under load). The
        # construction-time synchronous poll_once is untouched, so the counts
        # below reflect construction alone.
        #
        # Patched on parapetai_agent.control_plane, not on maf: the poller is
        # started inside control_plane.bootstrap_engine (shared with
        # Governor.from_control_plane) and resolves the name in its OWN
        # module, so patching maf's namespace would silently miss and let the
        # real daemon thread race this assertion again.
        import parapetai_agent.control_plane as cp_module

        monkeypatch.setattr(cp_module, "run_bundle_poller", lambda *a, **k: None)

        policy_dir = tmp_path / "policies"
        route = respx.get("https://cp.example/api/v1/bundle").mock(
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
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        keys_route = respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )

        for _ in range(3):
            build_middleware(
                agent_id="repeat-test",
                control_plane_url="https://cp.example",
                agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
                pep_key_path=tmp_path / "pep.key",
                persist_policy_dir=policy_dir,
            )

        # The real proof: 3 calls, ONE fetch -- if each call redid the
        # setup (the bug this registry fixes), this would be 3.
        assert route.call_count == 1
        assert keys_route.call_count == 1


class TestOtelAutoWiring:
    """build_middleware() calling configure_otel() FOR you once a control
    plane is configured -- see maf.py's own docstring, "OpenTelemetry is
    wired up automatically too". conftest.py's session-wide
    _reset_otel_module_state autouse fixture keeps these isolated from
    every other test in the package, same as TestOtelLogMode's own tests."""

    def _mock_control_plane(self) -> None:
        """Registers routes on the CURRENTLY ACTIVE respx router -- must
        only be called from inside a test already decorated with
        @respx.mock, not decorated with its own, or routes would land on
        a separate, nested mock context the outer test's real HTTP calls
        never see."""
        respx.get("https://cp.example/api/v1/bundle").mock(
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
        respx.post("https://cp.example/api/v1/fleet/heartbeat").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        respx.post("https://cp.example/api/v1/keys").mock(
            return_value=Response(200, json={"status": "registered"})
        )

    @respx.mock
    def test_control_plane_configured_auto_configures_otel(self) -> None:
        import parapetai_agent.maf as maf_module

        assert maf_module._otel_tracer_provider is None
        self._mock_control_plane()
        build_middleware(
            agent_id="otel-auto-1",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
        )
        assert maf_module._otel_tracer_provider is not None
        assert maf_module._otel_logger is not None
        # Stop the background bundle-poll thread WHILE respx's mock is
        # still active (this decorated function is still executing) --
        # otherwise it outlives this test's own @respx.mock scope (which
        # un-mocks the instant this function returns, before pytest's
        # own autouse-fixture teardown gets a chance to run
        # reset_middleware_registry() itself) and its next poll cycle
        # hits a REAL, unmocked network call. Same reasoning as
        # TestBuildMiddlewareIdentityRegistry's own
        # test_repeated_calls_with_control_plane_pull_only_once.
        reset_middleware_registry()

    @respx.mock
    def test_no_control_plane_leaves_otel_unconfigured(self) -> None:
        import parapetai_agent.maf as maf_module

        build_middleware(POLICIES, POLICIES / "entities.json", agent_id="otel-auto-2")
        assert maf_module._otel_tracer_provider is None

    @respx.mock
    def test_otel_log_mode_streaming_threads_through(self) -> None:
        import parapetai_agent.maf as maf_module

        self._mock_control_plane()
        build_middleware(
            agent_id="otel-auto-3",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            otel_log_mode="streaming",
        )
        reset_middleware_registry()  # see the first test's own comment on why
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = {type(p).__name__ for p in tp._active_span_processor._span_processors}
        # console=True's own SimpleSpanProcessor is always present alongside
        # whichever processor log_mode chose for the OTLP exporter -- see
        # configure_otel()'s own "console processor is always simple" note.
        assert processor_types == {"SimpleSpanProcessor"}

    @respx.mock
    def test_otel_log_mode_defaults_to_buffered(self) -> None:
        import parapetai_agent.maf as maf_module

        self._mock_control_plane()
        build_middleware(
            agent_id="otel-auto-4",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
        )
        reset_middleware_registry()  # see the first test's own comment on why
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = {type(p).__name__ for p in tp._active_span_processor._span_processors}
        assert processor_types == {"SimpleSpanProcessor", "BatchSpanProcessor"}

    @respx.mock
    def test_console_false_disables_the_auto_wired_otel_console_exporter(self) -> None:
        import parapetai_agent.maf as maf_module

        self._mock_control_plane()
        build_middleware(
            agent_id="otel-auto-6",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
            console=False,
        )
        reset_middleware_registry()  # see the first test's own comment on why
        tp = maf_module._otel_tracer_provider
        assert tp is not None
        processor_types = {type(p).__name__ for p in tp._active_span_processor._span_processors}
        # No ConsoleSpanExporter-backed SimpleSpanProcessor -- only the
        # OTLP exporter's own (buffered, by default) processor.
        assert processor_types == {"BatchSpanProcessor"}

    @respx.mock
    def test_an_earlier_explicit_configure_otel_call_wins(self) -> None:
        """The embedder's own, earlier configure_otel() call (e.g. for
        Azure Monitor export, like examples/maf_webapp/web_app.py's
        lifespan()) must not be silently clobbered by this auto-wiring --
        first call wins, matching OTel's own set-once global registration."""
        import parapetai_agent.maf as maf_module

        maf_module.configure_otel(service_name="my-own-config", console=False)
        tp_before = maf_module._otel_tracer_provider
        assert tp_before is not None

        self._mock_control_plane()
        build_middleware(
            agent_id="otel-auto-5",
            control_plane_url="https://cp.example",
            agent_secret="the-secret",  # noqa: S106 -- test fixture, not a real credential
        )

        # Still the SAME provider instance -- auto-wiring stepped aside
        # entirely rather than reconfiguring over it.
        assert maf_module._otel_tracer_provider is tp_before
        reset_middleware_registry()  # see the first test's own comment on why


class TestLocalLogDir:
    """local_log_dir= on build_middleware()/GovernedAgent -- one less
    required call at the embedding application's own top level (was:
    a separate configure_rotating_audit_log() call before this existed).
    """

    def test_writes_the_audit_log_file(self, tmp_path: Path) -> None:
        build_middleware(agent_id="log-dir-test-1", local_log_dir=tmp_path)
        assert (tmp_path / "parapetai-decisions.jsonl").exists()

    def test_idempotent_across_multiple_calls_with_the_same_dir(self, tmp_path: Path) -> None:
        """Two GovernedAgents (different agent_id -> different registry
        entries) sharing one local_log_dir -- a real scenario (e.g. one
        per chat session in a web app) -- must not attach a duplicate
        pair of handlers, or every decision would be logged twice."""
        pkg_logger = logging.getLogger("parapetai_agent")
        before = len(pkg_logger.handlers)

        build_middleware(agent_id="log-dir-test-2a", local_log_dir=tmp_path)
        after_first = len(pkg_logger.handlers)
        build_middleware(agent_id="log-dir-test-2b", local_log_dir=tmp_path)
        after_second = len(pkg_logger.handlers)

        assert after_first == before + 2  # one file handler, one stream handler
        assert after_second == after_first  # second call, same dir: no new handlers

    def test_console_false_skips_the_stream_handler_but_still_writes_the_file(
        self, tmp_path: Path
    ) -> None:
        # logging.handlers.RotatingFileHandler IS itself a StreamHandler
        # subclass (FileHandler -> StreamHandler in the stdlib) -- so the
        # real proof isn't "no StreamHandler at all" but "no PLAIN one
        # (console output, not file-backed)" alongside the expected file
        # handler.
        pkg_logger = logging.getLogger("parapetai_agent")
        before = len(pkg_logger.handlers)

        build_middleware(agent_id="log-dir-test-3", local_log_dir=tmp_path, console=False)

        new_handlers = pkg_logger.handlers[before:]
        assert len(new_handlers) == 1  # file handler only, no separate stream handler
        assert isinstance(new_handlers[0], logging.FileHandler)
        assert (tmp_path / "parapetai-decisions.jsonl").exists()

    def test_calling_configure_rotating_audit_log_directly_is_also_idempotent(
        self, tmp_path: Path
    ) -> None:
        path1 = configure_rotating_audit_log(tmp_path)
        path2 = configure_rotating_audit_log(tmp_path)
        assert path1 == path2 == tmp_path / "parapetai-decisions.jsonl"


class TestIdentityFromAzureCredential:
    """identity_from_azure_credential() -- the same decode path
    identity_from_bearer_token() already has, sourced from a
    TokenCredential's own get_token() instead of a token already in
    hand. A fake credential stub (get_token returning a real, decodable
    synthetic JWT via _make_jwt) proves the wiring without needing a real
    `az login` or network call."""

    def test_extracts_identity_from_the_credentials_token(self) -> None:
        token = _make_jwt(
            {
                "oid": "alice-oid",
                "preferred_username": "alice@contoso.com",
                "roles": ["OrderViewer"],
            }
        )

        class _FakeAccessToken:
            def __init__(self, token: str) -> None:
                self.token = token

        class _FakeCredential:
            def __init__(self, token: str) -> None:
                self._token = token
                self.requested_scopes: list[str] = []

            def get_token(self, *scopes: str, **kwargs: object) -> _FakeAccessToken:
                self.requested_scopes.extend(scopes)
                return _FakeAccessToken(self._token)

        credential = _FakeCredential(token)
        with identity_from_azure_credential(credential):  # type: ignore[arg-type]
            claims = _identity_claims(None)
            roles = _identity_roles(None)

        assert claims.get("oid") == "alice-oid"
        assert roles == ["OrderViewer"]
        # Default scope is Cognitive Services (what Foundry itself
        # calls) unless overridden -- confirms the default actually got
        # passed through to get_token(), not silently dropped.
        assert credential.requested_scopes == ["https://cognitiveservices.azure.com/.default"]

    def test_custom_scope_is_passed_through(self) -> None:
        token = _make_jwt({"oid": "x"})

        class _FakeAccessToken:
            def __init__(self, token: str) -> None:
                self.token = token

        class _FakeCredential:
            def __init__(self, token: str) -> None:
                self._token = token
                self.requested_scopes: list[str] = []

            def get_token(self, *scopes: str, **kwargs: object) -> _FakeAccessToken:
                self.requested_scopes.extend(scopes)
                return _FakeAccessToken(self._token)

        credential = _FakeCredential(token)
        with identity_from_azure_credential(
            credential,  # type: ignore[arg-type]
            scope="https://management.azure.com/.default",
        ):
            pass

        assert credential.requested_scopes == ["https://management.azure.com/.default"]


class TestGovernedIdentity:
    """governed_identity() -- one context manager dispatching to
    current_identity()/identity_from_bearer_token()/
    identity_from_azure_credential() based on which single source kwarg
    was given, so the caller never has to pick which of the three
    underlying functions matches the shape of their identity data."""

    def test_claims_and_roles_source(self) -> None:
        with governed_identity(claims={"oid": "alice"}, roles=["OrderViewer"]):
            assert _identity_claims(None) == {"oid": "alice"}
            assert _identity_roles(None) == ["OrderViewer"]

    def test_roles_only_still_counts_as_a_source(self) -> None:
        # claims omitted entirely (not even None passed) -- roles= alone
        # must still be recognised as "a source was given", not treated
        # as the zero-sources error case.
        with governed_identity(roles=["OrderViewer"]):
            assert _identity_roles(None) == ["OrderViewer"]

    def test_token_source(self) -> None:
        token = _make_jwt({"oid": "bob", "roles": ["OrderViewer"]})
        with governed_identity(token=token):
            assert _identity_claims(None).get("oid") == "bob"
            assert _identity_roles(None) == ["OrderViewer"]

    def test_credential_source(self) -> None:
        token = _make_jwt({"oid": "carol", "roles": ["OrderViewer"]})

        class _FakeAccessToken:
            def __init__(self, token: str) -> None:
                self.token = token

        class _FakeCredential:
            def get_token(self, *scopes: str, **kwargs: object) -> _FakeAccessToken:
                return _FakeAccessToken(token)

        with governed_identity(credential=_FakeCredential()):  # type: ignore[arg-type]
            assert _identity_claims(None).get("oid") == "carol"

    def test_zero_sources_raises_immediately(self) -> None:
        with pytest.raises(ValueError, match="needs exactly one identity source"):
            with governed_identity():
                pass

    def test_more_than_one_source_raises_immediately(self) -> None:
        token = _make_jwt({"oid": "x"})
        with pytest.raises(ValueError, match="more than one identity source"):
            with governed_identity(claims={"oid": "y"}, token=token):
                pass

    def test_identity_restored_after_the_block_regardless_of_which_source(self) -> None:
        assert _identity_claims(None) == {}
        with governed_identity(claims={"oid": "alice"}):
            assert _identity_claims(None) == {"oid": "alice"}
        assert _identity_claims(None) == {}
