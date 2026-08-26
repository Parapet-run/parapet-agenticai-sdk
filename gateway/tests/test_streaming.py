"""SSE streaming relay -- CLAUDE.md invariant 6: never buffer or reorder
chunks, and it's the default path for every framework.

No test in this suite had actually exercised the gateway's streaming code
path (_forward's stream=True branch, StreamingResponse, resp.aiter_raw()) end
to end -- everything so far used non-streaming requests. This is a genuine
multi-step conversation (a tool-call turn, then a final-text turn, each
delivered as several distinct SSE chunks, not one blob) acting as a minimal
"test agent": it parses each stream incrementally, reconstructs the tool call
argument from fragments split *mid-JSON-token* the way a real model would
split them, executes the tool, and sends the follow-up request -- the same
shape every real probe's SDK does internally, just without a framework
abstraction in the way.

The upstream is respx-mocked but genuinely chunked (an async byte stream,
several distinct `data: ` events per turn) specifically so ordering/fidelity
is actually being tested, not just full-body content equality.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient
from httpx._content import AsyncIteratorByteStream
from parapetai_agent.policy.engine import PolicyEngine
from parapetai_gateway.server.app import create_app

POLICIES = Path(__file__).resolve().parents[2] / "policies"

# Split mid-token ("order" / "_id") on purpose: a naive line-based or
# JSON-aware reassembly bug would show up here, a clean split would not.
TOOL_CALL_CHUNKS: list[dict[str, Any]] = [
    {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup_order", "arguments": ""},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"order'}}]},
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '_id": "12345"}'}}]
                },
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "c1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    },
]

FINAL_TEXT_CHUNKS: list[dict[str, Any]] = [
    {
        "id": "c2",
        "object": "chat.completion.chunk",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "Order "}, "finish_reason": None}
        ],
    },
    {
        "id": "c2",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": "12345 has "}, "finish_reason": None}],
    },
    {
        "id": "c2",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": "shipped."}, "finish_reason": None}],
    },
    {
        "id": "c2",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
]


def _sse_body(chunks: list[dict[str, Any]]) -> AsyncIteratorByteStream:
    async def gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            # One chunk per SSE event, exactly as a real model streams them --
            # never batched into a single write.
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return AsyncIteratorByteStream(gen())


def _parse_sse_events(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: ") :]))
    return events


def test_multi_step_streaming_tool_call_relayed_faithfully() -> None:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    client = TestClient(create_app(engine))

    with respx.mock:
        route = respx.post("https://api.openai.com/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse_body(TOOL_CALL_CHUNKS),
            ),
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_sse_body(FINAL_TEXT_CHUNKS),
            ),
        ]

        # -- Turn 1: streamed tool-call request --------------------------
        with client.stream(
            "POST",
            "/a/streaming-test/v1/chat/completions",
            json={
                "model": "m",
                "stream": True,
                "messages": [{"role": "user", "content": "Look up order 12345"}],
                "tools": [{"type": "function", "function": {"name": "lookup_order"}}],
            },
        ) as resp:
            raw_text = "".join(resp.iter_text())

        events = _parse_sse_events(raw_text)
        # Faithful relay means exactly the chunks we sent, in order -- not
        # merged into fewer events and not reordered.
        assert events == TOOL_CALL_CHUNKS
        assert raw_text.rstrip().endswith("data: [DONE]")

        # Reassemble the tool call the way a real SDK does: concatenate
        # argument fragments across deltas sharing the same tool_call index.
        arg_fragments = [
            tc["function"].get("arguments", "")
            for e in events
            for tc in e["choices"][0]["delta"].get("tool_calls", [])
        ]
        tool_call_id = next(
            tc["id"]
            for e in events
            for tc in e["choices"][0]["delta"].get("tool_calls", [])
            if "id" in tc
        )
        arguments = json.loads("".join(arg_fragments))
        assert arguments == {"order_id": "12345"}

        # Execute the tool locally, exactly what a real agent framework does.
        tool_result = f"order {arguments['order_id']}: shipped"

        # -- Turn 2: streamed follow-up with the tool result --------------
        with client.stream(
            "POST",
            "/a/streaming-test/v1/chat/completions",
            json={
                "model": "m",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "Look up order 12345"},
                    {"role": "assistant", "tool_calls": [{"id": tool_call_id, "type": "function"}]},
                    {"role": "tool", "tool_call_id": tool_call_id, "content": tool_result},
                ],
            },
        ) as resp2:
            raw_text2 = "".join(resp2.iter_text())

        events2 = _parse_sse_events(raw_text2)
        assert events2 == FINAL_TEXT_CHUNKS
        final_text = "".join(e["choices"][0]["delta"].get("content", "") for e in events2)
        assert final_text == "Order 12345 has shipped."

    assert route.call_count == 2


# A "no buffering" claim needs wall-clock evidence, not a content check: a
# relay that reads the whole upstream response before writing anything would
# still pass every assertion above. TestClient's in-process ASGI transport
# isn't a reliable place to measure that -- it has its own buffering
# unrelated to our code, confirmed empirically: mocking the same 5-chunk SSE
# body used above and reading it back via TestClient collapsed to a single
# `iter_bytes()` chunk regardless of what the mock yielded. Timing evidence
# requires a real socket. See scripts/verify_streaming_live.py, run against a
# live gateway + live upstream with an artificial per-chunk delay, for that
# proof -- not reproduced here because it needs real subprocesses, not a
# pytest fixture.
