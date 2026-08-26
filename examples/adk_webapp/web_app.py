"""Live example: a real ADK agent behind a real FastAPI web server, with
verified end-user identity flowing into Cedar -- the answer to "where does
Parapet get identity from for a web-deployed ADK agent" (see adk_sample_01/
for the CLI-shaped answer, and this module's own comments for why a web
deployment needs a genuinely different mechanism, not just a bigger
version of the same one).

Confirmed live, not assumed, before building this (see the conversation
this was built from): ADK itself has no standard place end-user identity
comes from --

  - `Session.user_id` is a plain, UNVERIFIED string every run_async() call
    must supply; ADK never authenticates it (confirmed against
    google-adk's own runners.py -- it's part of the session storage
    lookup key, not a checked credential).
  - `adk web`'s own REST endpoints are unauthenticated by the CLI's own
    documented design ("run it on a trusted network only") -- `user_id`
    there is a bare, uncontrolled URL path segment.
  - Gemini Enterprise's own connector/`temp:<AUTH_ID>` injection
    (see e.g. google/adk-samples' adk-ae-oauth sample) is a DIFFERENT
    thing: a tool's own delegated credential for calling a third-party
    API (Google Drive, GitHub), not a signal about who's calling YOUR
    agent.

The one place verified identity genuinely can originate for a web
deployment is the HTTP layer, before ADK ever sees the request --
`google.adk.cli.fast_api.get_fast_api_app()` returns a real `FastAPI`
(built directly on Starlette), so `parapetai_agent.identity_middleware.
IdentityMiddleware` -- already built, previously only demonstrated by
`examples/maf_webapp/` -- works here UNCHANGED. This script hand-rolls
its own minimal FastAPI app (one `/chat` route calling `GovernedRunner.
run_async()` directly) instead of `get_fast_api_app()`'s own agents-dir/
extra_plugins mechanism, specifically so the whole identity-middleware ->
governed-call chain runs in one coroutine this file fully controls --
verified below, not left to ADK's own internal worker-task plumbing
(`api_server.py`'s `/run` endpoint uses `asyncio.create_task()`
internally; contextvar propagation across that boundary was not
something this example wanted to depend on without checking).

WIRING -- the two lines that matter:

    app.add_middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())
    runner = GovernedRunner(agent=root_agent, app_name=..., session_service=...)

Everything else in this file is the demo scaffolding around those two
lines (the agent, the tool, the /chat route, session bookkeeping).

Run:
    cp examples/adk_webapp/.env.example examples/adk_webapp/.env
    # edit .env: fill in GOOGLE_API_KEY
    uv run --extra adk --extra web python3 examples/adk_webapp/web_app.py

Then, in another shell (see mint_demo_jwt.py for the tokens):
    curl -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" \\
        -d '{"user_id": "alice", "message": "Look up order 12345"}'
    # -> tool call ALLOWED or DENIED depending on which token, if any, you
    #    send in the Authorization header -- see README.md for the three
    #    scenarios this demonstrates.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from parapetai_agent.adk import GovernedRunner, track_tool_denials
from parapetai_agent.identity_middleware import IdentityMiddleware, jwt_bearer_extractor

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

APP_NAME = "order-support"
# See adk_sample_01/run_example.py's own comment on why this default
# needs an env-var escape hatch -- Google retires Gemini model ids faster
# than this file gets updated.
MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.6-flash")


def lookup_order(order_id: str) -> str:
    """Look up the status of a customer order.

    Use this tool when the customer asks about an order's status.

    Args:
        order_id: The order identifier to look up.

    Returns:
        The order's current status.
    """
    return f"order {order_id}: shipped"


root_agent = Agent(
    name="OrderSupportAgent",
    model=MODEL,
    instruction=(
        "You are a customer support agent. Use the lookup_order tool to check "
        "order status when asked. Keep replies brief."
    ),
    tools=[lookup_order],
)

session_service = InMemorySessionService()

# <create_runner>
runner = GovernedRunner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=session_service,
    local_log_dir=EXAMPLE_DIR / "logs",
    console=False,  # write the audit log to logs/, don't also print it here
    # trust_session_user_id intentionally left at its default (False):
    # THIS app has a real identity source (IdentityMiddleware, below), so
    # the unverified Session.user_id fallback should stay off -- see
    # adk.py's own "Identity" section, and contrast adk_sample_01/, which
    # opts it on because it's a single-operator CLI script with nothing
    # to protect against by not verifying "alice".
)
# </create_runner>

app = FastAPI(title="adk_webapp demo")

# <add_identity_middleware>
# Lifts whatever JWT arrives as `Authorization: Bearer <token>` into
# ambient identity for the duration of the request -- every governed call
# made anywhere inside the /chat handler below (in fact, anywhere in the
# whole process, for the life of this request) sees it automatically, no
# `with governed_identity(...):` needed at the call site. See
# mint_demo_jwt.py for how to produce a token this'll decode.
app.add_middleware(IdentityMiddleware, extractor=jwt_bearer_extractor())
# </add_identity_middleware>


class ChatRequest(BaseModel):
    user_id: str
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    reply: str
    tool_denied: bool
    tool_denial_reasons: list[str]


async def _ensure_session(user_id: str, session_id: str) -> None:
    existing = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if existing is None:
        await session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    await _ensure_session(req.user_id, req.session_id)

    reply_parts: list[str] = []
    # track_tool_denials() (parapetai_agent.governance_runtime, shared with
    # maf.py) is the deterministic way to know a tool call was blocked --
    # a denied tool_call substitutes its RESULT (see ParapetPlugin.
    # before_tool_callback's own docstring: it becomes the tool's return
    # value, per BasePlugin's documented contract), it does not raise, and
    # nothing guarantees the model's own final reply faithfully reports it
    # rather than paraphrasing around it.
    with track_tool_denials() as denials:
        async for event in runner.run_async(
            user_id=req.user_id,
            session_id=req.session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=req.message)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        reply_parts.append(part.text)

    return ChatResponse(
        reply="".join(reply_parts), tool_denied=bool(denials), tool_denial_reasons=list(denials)
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
