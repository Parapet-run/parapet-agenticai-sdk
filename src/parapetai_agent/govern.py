"""Framework-neutral governance — govern any agent loop, no framework required.

`parapetai_agent.maf` wires governance into the Microsoft Agent Framework's
middleware. This module does the same job with **no framework at all**: three
explicit calls any agent loop, tool wrapper, or framework callback can make.

    from parapetai_agent import Governor, GovernanceDenied

    gov = Governor.from_policy_dir("./policies")

    gov.check_input(prompt, roles=["OrderViewer"])   # before the model
    gov.authorize_tool("delete_incident", {...})     # before a tool runs -> may raise
    gov.check_output(answer, sources=[doc])          # after the model

Under the hood this is the SAME decision path `maf.py` uses — a
`GovernanceHook` over a `PolicyEngine` — so a decision made here is identical
to one made through the MAF adapter. Adding governance to LangGraph, CrewAI,
the OpenAI Agents SDK, or a plain `while` loop is just calling these methods at
that framework's own tool/model hook points.

Every method returns the Cedar `Decision`. By default a deny raises
`GovernanceDenied` (so a denied tool call never reaches your tool); pass
`raise_on_deny=False` to get the `Decision` back and branch on it yourself.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from parapetai_agent._exceptions import GovernanceDenied
from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.groundedness import GroundednessConfig
from parapetai_agent.identity import Caller
from parapetai_agent.policy.engine import Decision, PolicyEngine
from parapetai_agent.policy.hooks import GovernanceHook, OnDecision
from parapetai_agent.providers.parsers import Snapshot
from parapetai_agent.response_judge import JudgeConfig

__all__ = ["Governor", "GovernanceDenied"]

# Chars of prompt/response kept SDK-side so scanners can see them. Never logged:
# the audit record is content-free (parapetai_agent.policy.hooks.content_free).
_PREVIEW = 4000
_PROVIDER = "govern"


class Governor:
    """A framework-neutral governance entry point over one policy set.

    Construct it once (from a local policy dir, or an in-memory bundle), then
    call `check_input` / `authorize_tool` / `check_output` from wherever your
    agent framework fires. Identity is passed per call (`roles=`, `claims=`);
    with none supplied the caller is unauthenticated, which Cedar evaluates
    under its default-deny policy set — never a bypass.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        caller: Caller | None = None,
        content_checks: ContentCheckConfig | None = None,
        groundedness: GroundednessConfig | None = None,
        judge: JudgeConfig | None = None,
        on_decision: OnDecision | None = None,
    ) -> None:
        self._engine = engine
        self._caller = caller or Caller(agent_id="agent")
        self._hook = GovernanceHook(engine, self._caller, on_decision=on_decision)
        self._content_checks = content_checks
        self._groundedness = groundedness
        self._judge = judge

    # ------------------------------------------------------------------ #
    # constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def from_policy_dir(
        cls,
        policy_dir: str | Path,
        entities_path: str | Path | None = None,
        *,
        bundle_files: Mapping[str, str] | None = None,
        caller: Caller | None = None,
        on_decision: OnDecision | None = None,
    ) -> Governor:
        """Load Cedar policy from local files. `bundle_files` optionally supplies
        the content-check / groundedness / judge JSON configs (the same files a
        control-plane bundle carries) to enable the input scanners and output
        evals; without them, only Cedar authorization runs."""
        engine = PolicyEngine(policy_dir, entities_path)
        cc, gr, jd = ContentCheckConfig(), GroundednessConfig(), JudgeConfig()
        if bundle_files:
            files = dict(bundle_files)
            cc.load_from_bundle(files)
            gr.load_from_bundle(files)
            jd.load_from_bundle(files)
        return cls(
            engine,
            caller=caller,
            content_checks=cc,
            groundedness=gr,
            judge=jd,
            on_decision=on_decision,
        )

    # ------------------------------------------------------------------ #
    # the three decisions
    # ------------------------------------------------------------------ #
    def check_input(
        self,
        text: str,
        *,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        model: str | None = None,
        tools: Sequence[str] | None = None,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Pre-model guardrail: run any configured input scanners (PII, secrets,
        injection) and a Cedar `model_call` decision before the model sees the
        prompt."""
        claims_d, roles_l = self._identity(claims, roles)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:model_call",
            parsed=True,
            model=model,
            messages_preview=str(text)[:_PREVIEW],
            declared_tools=list(tools or []),
            identity_claims=claims_d,
            identity_roles=roles_l,
        )
        extra: dict[str, Any] = {}
        if self._content_checks is not None:
            # evaluate() is a harmless no-op when no scanners are configured.
            res = self._content_checks.evaluate(snap)
            if res.errors:  # a configured scanner could not run -> fail closed
                return self._deny(self._failure_decision(res.errors), raise_on_deny)
            extra = res.context
        result = self._hook.evaluate(snapshot=snap, stage="pre", extra_context=extra or None)
        return self._finish(result.decision, raise_on_deny)

    def authorize_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Authorize one tool call — by name, arguments, and caller role —
        against Cedar, before it executes. A denied call raises (default) so it
        never runs."""
        claims_d, roles_l = self._identity(claims, roles)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:tool_call",
            parsed=True,
            tool_name=name,
            tool_args=dict(arguments or {}),
            identity_claims=claims_d,
            identity_roles=roles_l,
        )
        result = self._hook.evaluate(snapshot=snap, stage="pre")
        return self._finish(result.decision, raise_on_deny)

    def check_output(
        self,
        response: str,
        *,
        sources: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        claims: Mapping[str, Any] | None = None,
        model: str | None = None,
        raise_on_deny: bool = True,
    ) -> Decision:
        """Post-model eval: score groundedness (against `sources`) and run the
        SLM judge if configured, then a Cedar `post` decision — before the
        answer is delivered. A scorer that errors fails closed (denies)."""
        claims_d, roles_l = self._identity(claims, roles)
        snap = Snapshot(
            provider=_PROVIDER,
            endpoint="in-process:govern:model_call",
            parsed=True,
            model=model,
            response_preview=str(response)[:_PREVIEW],
            identity_claims=claims_d,
            identity_roles=roles_l,
        )
        extra: dict[str, Any] = {}
        errors: list[str] = []
        source_text = " ".join(s for s in sources if s) if sources else ""
        if self._groundedness is not None and self._groundedness.active and source_text:
            g = self._groundedness.evaluate_post(str(response), source_text)
            errors.extend(g.errors)
            extra.update(g.context)
        if self._judge is not None and self._judge.active:
            j = self._judge.evaluate_post(str(response))
            errors.extend(j.errors)
            extra.update(j.context)
        if errors:  # a scorer could not run -> fail closed
            return self._deny(self._failure_decision(tuple(errors)), raise_on_deny)
        result = self._hook.evaluate(snapshot=snap, stage="post", extra_context=extra or None)
        return self._finish(result.decision, raise_on_deny)

    # ------------------------------------------------------------------ #
    # convenience: a decorator that authorizes a tool before it runs
    # ------------------------------------------------------------------ #
    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
    ) -> Callable[..., Any]:
        """Wrap a tool function so it is authorized (by name + keyword args)
        before it runs; a denial raises `GovernanceDenied` and the body never
        executes. Works on sync and async functions.

            @gov.tool
            def delete_incident(number: str) -> str: ...
        """

        def deco(f: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = str(name or getattr(f, "__name__", "tool"))
            if inspect.iscoroutinefunction(f):

                @functools.wraps(f)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    self.authorize_tool(tool_name, kwargs)
                    return await f(*args, **kwargs)

                return awrapper

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.authorize_tool(tool_name, kwargs)
                return f(*args, **kwargs)

            return wrapper

        return deco(fn) if fn is not None else deco

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _identity(
        claims: Mapping[str, Any] | None, roles: Sequence[str] | None
    ) -> tuple[dict[str, str], list[str]]:
        if claims is None and roles is None:
            return {}, []  # unauthenticated: Cedar sees no identity_roles
        return (
            {str(k): str(v) for k, v in (claims or {}).items()},
            [str(r) for r in (roles or [])],
        )

    def _failure_decision(self, errors: tuple[str, ...]) -> Decision:
        try:
            gen = int(self._engine.status.get("generation", 0))
        except Exception:  # noqa: BLE001 -- generation is audit-only here
            gen = 0
        reason = f"content check scanner failure: {'; '.join(errors)}"
        return Decision(False, "deny", reason, gen, 0.0, errors=tuple(errors))

    def _finish(self, decision: Decision, raise_on_deny: bool) -> Decision:
        if raise_on_deny and not decision.allowed:
            raise GovernanceDenied(decision)
        return decision

    def _deny(self, decision: Decision, raise_on_deny: bool) -> Decision:
        if raise_on_deny:
            raise GovernanceDenied(decision)
        return decision
