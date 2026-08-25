"""Canned OpenAI-shaped upstream for CI conformance runs.

Stands in for a real model provider so `make conformance` can run hermetically
-- no Groq/OpenAI credentials, no network egress, no cost, no flakiness from a
real model's non-determinism. It proves the same thing the real run proves
(the env var routes traffic through the gateway to *an* upstream and a
tool-calling round trip completes) without needing a live provider.

Turn logic, keyed off whether a tool result is already present in the
request: no tool result + tools declared -> emit a tool_call for the first
declared tool; tool result present -> emit a final text answer. This matches
the one-tool-call shape every conformance probe uses (see
conformance/frameworks/*/probe.py).

This is a test fixture, not a product surface: it does not implement policy,
auth, or anything beyond the two response shapes (`/v1/chat/completions`,
non-streaming and streaming) the probes actually exercise. All four probes use
Chat Completions, not the Responses API -- see the openai-agents probe for why.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-upstream")

# Opt-in only, 0 by default -- unrelated to normal conformance runs. Exists so
# a live run can prove the gateway relays chunks as they arrive rather than
# buffering the whole stream, which needs real wall-clock gaps between
# writes on a real socket to observe at all. See scripts/verify_streaming_live.py.
_STREAM_DELAY_S = int(os.environ.get("FAKE_UPSTREAM_STREAM_DELAY_MS", "0")) / 1000

_DEFAULT_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "lookup_order": {"order_id": "12345"},
    # Any tool name not listed here gets {} args (see _completion/_stream_chunks
    # below) -- fine for a tool with no required parameters, but a tool with a
    # required parameter then fails argument-schema validation on the CLIENT
    # side, before the call ever reaches the caller's own middleware/policy
    # layer. That looks identical to a real denial (no call reaches the real
    # tool either way) unless something logs the actual decision -- found via
    # exactly that confusion in examples/maf_webapp/run_example.py,
    # whose deny-scenario check was passing for the wrong reason until Cedar
    # decision audit logging (parapetai-agent/src/parapetai_agent/maf.py's
    # configure_rotating_audit_log) made the missing tool_call decision visible.
    "execute_shell": {"command": "echo hi"},
    "get_forecast": {"latitude": 47.6062, "longitude": -122.3321},
}


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "tool" for m in messages)


def _first_tool_name(tools: list[dict[str, Any]]) -> str | None:
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name"):
            return str(fn["name"])
    return None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    model = body.get("model", "fake-model")
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    stream = bool(body.get("stream"))

    tool_name = None if _has_tool_result(messages) else _first_tool_name(tools)

    if stream:
        return StreamingResponse(_stream_chunks(model, tool_name), media_type="text/event-stream")
    return JSONResponse(_completion(model, tool_name))


def _completion(model: str, tool_name: str | None) -> dict[str, Any]:
    base = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    if tool_name:
        args = json.dumps(_DEFAULT_TOOL_ARGS.get(tool_name, {}))
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    # A fresh id per response, not a shared literal: some
                    # client-side tool-calling loops treat the id as a
                    # global key, not a per-conversation one, and silently
                    # skip invoking a tool whose call id they've already
                    # seen under a different name -- found via a client
                    # that ran three sequential, differently-named
                    # tool-calling turns against this fixture in one
                    # process; only the first ever actually invoked.
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args},
                }
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": "Order 12345 has shipped."}
        finish_reason = "stop"
    base["choices"] = [{"index": 0, "message": message, "finish_reason": finish_reason}]
    return base


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_chunks(model: str, tool_name: str | None) -> AsyncIterator[str]:
    head = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    if tool_name:
        args = json.dumps(_DEFAULT_TOOL_ARGS.get(tool_name, {}))
        call_id = f"call_{uuid.uuid4().hex[:12]}"  # see _completion's comment on why this must vary
        payloads = [
            {
                **head,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": tool_name, "arguments": ""},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                **head,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args}}]},
                        "finish_reason": None,
                    }
                ],
            },
            {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
    else:
        payloads = [
            {
                **head,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Order 12345 has shipped."},
                        "finish_reason": None,
                    }
                ],
            },
            {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    for payload in payloads:
        if _STREAM_DELAY_S:
            await asyncio.sleep(_STREAM_DELAY_S)
        yield _sse(payload)
    yield "data: [DONE]\n\n"


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9001)  # noqa: S104 -- test fixture, container-local only
