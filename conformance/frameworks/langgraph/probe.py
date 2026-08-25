"""Minimal LangGraph agent with one tool. No gateway-specific code -- that is the test."""

import os

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"order {order_id}: shipped"


model = ChatOpenAI(model=os.environ["CONFORMANCE_MODEL"])
agent = create_react_agent(model, tools=[lookup_order], prompt="Use the tool.")

result = agent.invoke({"messages": [{"role": "user", "content": "Look up order 12345"}]})
print(result["messages"][-1].content)
