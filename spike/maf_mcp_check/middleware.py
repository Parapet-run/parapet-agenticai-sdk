"""Shared FunctionMiddleware for the MAF middleware spike.

Always logs; conditionally blocks by tool name (question 3) without touching
the logging behavior otherwise -- one class, parametrized, matches "Register
a FunctionMiddleware that logs every intercepted call" while still letting
individual runs test the not-calling-call_next() path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware

BLOCKED_SENTINEL = "BLOCKED_BY_MIDDLEWARE"


class LoggingFunctionMiddleware(FunctionMiddleware):
    def __init__(self, blocked: set[str] | None = None) -> None:
        self.blocked = blocked or set()

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        fn = context.function
        print(f"[middleware] PRE  name={fn.name!r} kind={getattr(fn, 'kind', None)!r}")
        print(f"[middleware] PRE  arguments={context.arguments!r}")
        print(f"[middleware] PRE  type(function)={type(fn)!r}")
        props = getattr(fn, "additional_properties", None)
        print(f"[middleware] PRE  additional_properties={props!r}")

        if fn.name in self.blocked:
            print(f"[middleware] BLOCKING {fn.name!r} -- not calling call_next()")
            context.result = BLOCKED_SENTINEL
            return

        await call_next()
        print(f"[middleware] POST name={fn.name!r} result={context.result!r}")
