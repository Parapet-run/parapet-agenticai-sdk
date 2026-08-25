"""Authorize tool calls against a Cedar policy bundle — no model, no network.

Runs on the base install (`pip install parapetai-agent`). It loads the example
policies in ../policies and asks the engine to authorize a few tool calls the
way the SDK does at runtime, showing allow vs deny and which policy decided.

    python examples/authorize_tool_calls.py
"""

from __future__ import annotations

from pathlib import Path

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[1] / "policies"


def authorize(engine: PolicyEngine, tool_name: str, **args: object) -> None:
    context: dict[str, object] = {"tool_name": tool_name}
    if args:
        context["tool_args"] = args
    decision = engine.evaluate(
        principal='Agent::"support-agent"',
        action="tool_call",
        resource=f'Resource::"{tool_name}"',
        context=context,
        stage="pre",
    )
    verdict = "ALLOW" if decision.allowed else "DENY "
    why = ", ".join(decision.determining_policies) or "(default-deny: no rule matched)"
    shown = f"{tool_name}({args})" if args else f"{tool_name}()"
    print(f"  {verdict}  {shown:<52} -> {why}")


def main() -> None:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    print(f"Loaded {POLICIES.name}/ — authorizing tool calls:\n")

    # Allowed: an ordinary read.
    authorize(engine, "lookup_order", order_id="A1001")

    # Denied by name: destructive tools are forbidden deterministically.
    authorize(engine, "delete_incident", number="INC0010026")
    authorize(engine, "execute_shell", command="rm -rf /")

    # Denied by argument: closing an incident via a raw state update.
    authorize(engine, "update_incident", number="INC0010026", state="closed")
    # Same tool, benign argument: allowed.
    authorize(engine, "update_incident", number="INC0010026", state="in_progress")

    print("\nEvery decision is deterministic, fail-closed, and content-free.")


if __name__ == "__main__":
    main()
