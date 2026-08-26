"""One native Python tool for the spike. No required args, so it works with
the fake upstream's {} fallback for any tool name it doesn't specifically
recognize (its canned args map only has an entry for "lookup_order").

_actually_called is a module-level flag, not something derived from the LLM's
claimed behavior -- proves whether the function genuinely executed,
independent of what any middleware or mocked model reports.
"""

from __future__ import annotations

_actually_called = False


def get_server_status() -> str:
    """Return a canned status string. A native, non-MCP tool."""
    global _actually_called
    _actually_called = True
    return "status: all systems nominal"


def was_actually_called() -> bool:
    return _actually_called


def reset() -> None:
    global _actually_called
    _actually_called = False
