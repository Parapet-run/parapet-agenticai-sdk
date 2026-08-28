"""A local, deterministic stand-in for a real OpenAI-compatible chat
completions endpoint, stdlib-only (no extra dependency to install for the
default, offline path). agent_framework.openai.OpenAIChatCompletionClient
talks to it exactly as it would talk to a real API -- only the base_url
changes -- so nothing about the agent code differs between mock and real.

Deterministic by keyword, not by an actual model decision: the first turn
routes to salesforce_lookup or hr_lookup based on words in the user's
message, the second turn (once a tool result is present) relays that
result as the final answer. This is intentional -- a demo of Cedar
enforcement should not also depend on an LLM's tool-choice being
reproducible run to run.

Used by both example_no_governance.py and example_governed.py whenever
_use_mock() (see either file) decides no real model is configured.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _has_tool_result(messages: list[dict]) -> bool:
    return any(m.get("role") == "tool" for m in messages)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 -- silence stderr spam
        pass

    def do_POST(self) -> None:  # noqa: N802 -- stdlib method name
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])

        if _has_tool_result(messages):
            tool_msg = next(m for m in reversed(messages) if m.get("role") == "tool")
            message = {
                "role": "assistant",
                "content": f"Here's what I found: {tool_msg.get('content')}",
                "tool_calls": None,
            }
            finish_reason = "stop"
        else:
            text = _last_user_text(messages).lower()
            if re.search(r"salesforce|opportunity|deal|pipeline", text):
                tool_name = "salesforce_lookup"
            elif re.search(r"\bhr\b|benefits|payroll|pto", text):
                tool_name = "hr_lookup"
            else:
                tool_name = "salesforce_lookup"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_mock_1",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps({"query": _last_user_text(messages)}),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"

        response = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "mock-model"),
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> HTTPServer:
    """Binds an ephemeral local port (127.0.0.1 only) and serves in a
    daemon thread -- the caller reads server.server_port for the base_url
    and never needs to call shutdown() explicitly (the process exiting is
    enough, same as any other daemon thread)."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
