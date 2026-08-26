"""Cross-framework conformance: apply govern() to a REAL agent in each
supported framework and prove the block happens.

Only the LLM is a stub (conformance/fake-upstream: it always calls the first
declared tool, then answers) -- the frameworks themselves run their real code.
Each framework gets the same two governed tools and the same two assertions:

  * ALLOW: `lookup_order` is permitted by the example policies -> its body runs.
  * DENY : `execute_shell` is forbidden (20-tools.cedar tool_destructive_denied)
           -> @gov.tool raises before the body, so the destructive action never
           happens, no matter how the framework surfaces the error.

We use lookup_order / execute_shell specifically because the fake upstream
supplies real arguments for them; a tool whose required arg it doesn't know
would fail client-side arg validation and *look* like a denial for the wrong
reason (see conformance/fake-upstream/app.py's own note).

Each framework test skips if that framework isn't installed, so the base dev
env stays light; CI installs them and runs the lot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from parapetai_agent import GovernanceDenied, Governor

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICIES = REPO_ROOT / "policies"
FAKE_URL = "http://127.0.0.1:9002"


@pytest.fixture(scope="module")
def fake_model() -> None:
    """A canned OpenAI-compatible model. Skips the whole module if uv or the
    server can't come up, rather than failing."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available for the fake-model fixture")
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv
        [
            uv, "run", "--no-project", "--with", "fastapi", "--with", "uvicorn",
            "python3", str(REPO_ROOT / "conformance" / "fake-upstream" / "app.py"),
        ],
        env={**os.environ, "PORT": "9002"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            httpx.get(f"{FAKE_URL}/v1/chat/completions", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("fake model did not start")
    yield
    proc.terminate()
    proc.wait(timeout=5)


def _governed_tools():
    """Two plain functions wrapped with @gov.tool, plus a dict recording which
    bodies actually executed. Identical across every framework — only how each
    framework *declares* a tool differs."""
    gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")
    ran = {"lookup_order": False, "execute_shell": False}

    @gov.tool
    def lookup_order(order_id: str) -> str:
        """Look up an order by its id."""
        ran["lookup_order"] = True
        return f"order {order_id}: shipped"

    @gov.tool
    def execute_shell(command: str) -> str:
        """Run a shell command."""
        ran["execute_shell"] = True
        return "shell output"

    return gov, ran, lookup_order, execute_shell


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", f"{FAKE_URL}/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_CHAT_COMPLETION_MODEL", "fake-model")


# --------------------------------------------------------------------------- #
# Microsoft Agent Framework -- the built-in drop-in adapter (GovernedAgent),
# MAF's own recommended integration. Governance wraps every tool automatically;
# tools stay plain functions (no @gov.tool needed).
# --------------------------------------------------------------------------- #
class TestMAF:
    @staticmethod
    def _plain_tools() -> tuple[dict[str, bool], object, object]:
        ran = {"lookup_order": False, "execute_shell": False}

        def lookup_order(order_id: str) -> str:
            ran["lookup_order"] = True
            return f"order {order_id}: shipped"

        def execute_shell(command: str) -> str:
            ran["execute_shell"] = True
            return "shell output"

        return ran, lookup_order, execute_shell

    async def test_allowed_tool_runs(
        self, fake_model: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("agent_framework")
        from agent_framework.openai import OpenAIChatCompletionClient

        from parapetai_agent import GovernedAgent

        _env(monkeypatch)
        ran, lookup_order, _ = self._plain_tools()
        async with GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="t",
            instructions="Use the tool.",
            tools=[lookup_order],
            policy_dir=POLICIES,
            entities_path=POLICIES / "entities.json",
            agent_id="conf-maf-allow",
        ) as agent:
            await agent.run("Look up order 12345.")
        assert ran["lookup_order"] is True

    async def test_denied_tool_is_blocked(
        self, fake_model: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("agent_framework")
        from agent_framework.openai import OpenAIChatCompletionClient

        from parapetai_agent import GovernedAgent

        _env(monkeypatch)
        ran, _, execute_shell = self._plain_tools()
        async with GovernedAgent(
            client=OpenAIChatCompletionClient(),
            name="t",
            instructions="Use the tool.",
            tools=[execute_shell],
            policy_dir=POLICIES,
            entities_path=POLICIES / "entities.json",
            agent_id="conf-maf-deny",
        ) as agent:
            await agent.run("Run a shell command.")
        assert ran["execute_shell"] is False  # blocked before the body ran


# --------------------------------------------------------------------------- #
# OpenAI Agents SDK
# --------------------------------------------------------------------------- #
class TestOpenAIAgentsSDK:
    @staticmethod
    def _model():
        from agents import OpenAIChatCompletionsModel  # chat completions, not Responses
        from openai import AsyncOpenAI

        return OpenAIChatCompletionsModel(
            model="fake-model",
            openai_client=AsyncOpenAI(base_url=f"{FAKE_URL}/v1", api_key="test-key"),
        )

    async def test_allowed_tool_runs(
        self, fake_model: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("agents")
        from agents import Agent, Runner, function_tool, set_tracing_disabled

        set_tracing_disabled(True)
        _gov, ran, lookup_order, _ = _governed_tools()
        tool = function_tool(lookup_order)
        agent = Agent(name="t", instructions="Use the tool.", tools=[tool], model=self._model())
        await Runner.run(agent, "Look up order 12345.")
        assert ran["lookup_order"] is True

    async def test_denied_tool_is_blocked(
        self, fake_model: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("agents")
        from agents import Agent, Runner, function_tool, set_tracing_disabled

        set_tracing_disabled(True)
        _gov, ran, _, execute_shell = _governed_tools()
        tool = function_tool(execute_shell)
        agent = Agent(name="t", instructions="Use the tool.", tools=[tool], model=self._model())
        await Runner.run(agent, "Run a shell command.")
        assert ran["execute_shell"] is False


# --------------------------------------------------------------------------- #
# LangGraph / LangChain -- @gov.tool under LangChain's @tool
# --------------------------------------------------------------------------- #
class TestLangGraph:
    @staticmethod
    def _agent(governed_fn: object) -> object:
        from langchain_core.tools import tool as lc_tool
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        model = ChatOpenAI(model="fake-model", base_url=f"{FAKE_URL}/v1", api_key="test-key")
        return create_react_agent(model, tools=[lc_tool(governed_fn)])

    async def test_allowed_tool_runs(self, fake_model: None) -> None:
        pytest.importorskip("langgraph")
        pytest.importorskip("langchain_openai")
        _gov, ran, lookup_order, _ = _governed_tools()
        agent = self._agent(lookup_order)
        await agent.ainvoke({"messages": [{"role": "user", "content": "Look up order 12345."}]})
        assert ran["lookup_order"] is True

    async def test_denied_tool_is_blocked(self, fake_model: None) -> None:
        pytest.importorskip("langgraph")
        pytest.importorskip("langchain_openai")
        _gov, ran, _, execute_shell = _governed_tools()
        agent = self._agent(execute_shell)
        try:  # LangGraph may capture the tool error or propagate it; either way
            await agent.ainvoke({"messages": [{"role": "user", "content": "Run a shell command."}]})
        except GovernanceDenied:
            pass
        assert ran["execute_shell"] is False  # the destructive body never ran


# --------------------------------------------------------------------------- #
# CrewAI -- @gov.tool under CrewAI's @tool. kickoff() is synchronous, so these
# are plain (non-async) tests.
# --------------------------------------------------------------------------- #
class TestCrewAI:
    @staticmethod
    def _crew(governed_fn: object, task_desc: str) -> object:
        from crewai import LLM, Agent, Crew, Task
        from crewai.tools import tool as crew_tool

        llm = LLM(model="openai/fake-model", base_url=f"{FAKE_URL}/v1", api_key="test-key")
        tool = crew_tool(governed_fn.__name__)(governed_fn)
        agent = Agent(
            role="assistant",
            goal="Use the available tool.",
            backstory="A test agent.",
            tools=[tool],
            llm=llm,
            verbose=False,
        )
        task = Task(description=task_desc, expected_output="done", agent=agent)
        return Crew(agents=[agent], tasks=[task], verbose=False)

    @staticmethod
    def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        monkeypatch.setenv("CREWAI_TELEMETRY_OPT_OUT", "true")

    def test_allowed_tool_runs(self, fake_model: None, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("crewai")
        self._quiet(monkeypatch)
        _gov, ran, lookup_order, _ = _governed_tools()
        self._crew(lookup_order, "Look up order 12345.").kickoff()
        assert ran["lookup_order"] is True

    def test_denied_tool_is_blocked(
        self, fake_model: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("crewai")
        self._quiet(monkeypatch)
        _gov, ran, _, execute_shell = _governed_tools()
        try:  # CrewAI may capture the tool error or propagate it; either way
            self._crew(execute_shell, "Run a shell command.").kickoff()
        except GovernanceDenied:
            pass
        assert ran["execute_shell"] is False  # the destructive body never ran
