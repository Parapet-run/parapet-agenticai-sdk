"""A raw LangGraph StateGraph (LangGraph's own canonical "add tools" tutorial
shape: https://github.com/langchain-ai/langgraph, docs/tutorials/get-started/
2-add-tools.md) with Parapet governing all THREE stages, not just the tool
call:

  - pre-model:  Governor.check_input() before the fake model ever sees the
                prompt (PII/secrets/injection scanning + Cedar model_call)
  - tool call:  @gov.tool on each tool function -- unauthorized calls raise
                and never run
  - post-model: Governor.check_output() would run the same way on the
                final answer (see the ADAPTING NOTE below for why this
                demo's second turn skips it)

This closes the gap the SDK's existing generic LangGraph path
(examples/same_prompt_every_framework/adapters/langgraph.py,
tests/test_conformance_frameworks.py's TestLangGraph) leaves open: both of
those only ever call @gov.tool, so a LangGraph agent's PROMPT and final
ANSWER are ungoverned no matter how many tools are wrapped. This example
shows what closing that gap looks like using ONLY primitives that already
exist in this SDK today (Governor.check_input/check_output/tool) -- no new
adapter code, because none has been built yet (see docs/frameworks/langgraph.md
for the plan).

No model key, no network: langgraph.prebuilt.ToolNode/tools_condition don't
care how a message was produced, so a scripted
langchain_core.language_models.fake_chat_models.GenericFakeChatModel plays
the model's part deterministically -- simpler than the fake-HTTP-server
approach the MAF/ADK examples use, since there's no real client protocol to
imitate here.

Requires: pip install langgraph  (langchain-core comes along as its own
dependency; nothing else).

Run:  uv run python examples/langgraph_tool_calling.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from parapetai_agent import GovernanceDenied, Governor

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES = REPO_ROOT / "policies"

# One Governor, fully local -- from_policy_dir() never makes a network call.
# Reuses this repo's own root policies/ fixtures (20-tools.cedar forbids
# execute_shell unconditionally), the same ones authorize_tool_calls.py and
# governed_maf_demo.py already use, so every example in this directory
# agrees on what "governed" means for these tool names.
gov = Governor.from_policy_dir(POLICIES, POLICIES / "entities.json")


@gov.tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


@gov.tool
def execute_shell(command: str) -> str:
    """Run a shell command."""
    return f"ran: {command}"  # never reached when denied -- @gov.tool raises first


TOOLS = [lc_tool(lookup_order), lc_tool(execute_shell)]


class State(TypedDict):
    messages: Annotated[list, add_messages]


def _build_graph(model: GenericFakeChatModel):
    def chatbot(state: State) -> dict:
        prompt = state["messages"][-1].content if state["messages"] else ""
        # ADAPTING NOTE: check_input() runs once per node visit, so it also
        # gates the SECOND chatbot turn (after the tool result comes back) --
        # exactly as it should, since that turn is a fresh call to the model
        # too. raise_on_deny=False here because a pre-model deny should fold
        # into the conversation, not blow up the whole graph.invoke() call
        # the way an unauthorized TOOL call intentionally does below.
        decision = gov.check_input(str(prompt), raise_on_deny=False)
        if not decision.allowed:
            return {"messages": [AIMessage(content=f"GOVERNANCE_DENIED: {decision.reason}")]}
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(State)
    builder.add_node("chatbot", chatbot)
    builder.add_node("tools", ToolNode(tools=TOOLS))
    builder.add_conditional_edges("chatbot", tools_condition)
    builder.add_edge("tools", "chatbot")
    builder.add_edge(START, "chatbot")
    return builder.compile()


def _run_scenario(name: str, tool_name: str, args: dict) -> None:
    # Scripted two-turn model: first call it to call a tool, then answer.
    # A real ChatOpenAI/ChatAnthropic model would decide this on its own --
    # this demo governs the SAME graph shape regardless of what the model
    # actually decides, which is the point.
    model = GenericFakeChatModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
                AIMessage(content="Done."),
            ]
        )
    )
    graph = _build_graph(model)
    try:
        result = graph.invoke({"messages": [{"role": "user", "content": name}]})
        print(f"[{name:<24}] ALLOWED -> {result['messages'][-1].content!r}")
    except GovernanceDenied as exc:
        # @gov.tool raises straight through ToolNode and graph.invoke() --
        # unlike MAF's tool-call layer, LangGraph's ToolNode does not catch
        # and fold this into a synthetic result; it propagates as a real
        # exception, verified live by this example, not assumed.
        print(
            f"[{name:<24}] DENIED  -> {exc.decision.reason} ({exc.decision.determining_policies})"
        )


def main() -> None:
    print("Governed LangGraph tool-calling -- lookup_order vs. execute_shell\n")
    _run_scenario("lookup_order", "lookup_order", {"order_id": "A1001"})
    _run_scenario("execute_shell", "execute_shell", {"command": "rm -rf /"})
    print(
        "\nSame StateGraph shape either way -- Cedar decided, the graph never "
        "special-cased which tool it was."
    )


if __name__ == "__main__":
    main()
