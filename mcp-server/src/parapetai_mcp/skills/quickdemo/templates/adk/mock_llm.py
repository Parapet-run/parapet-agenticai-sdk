"""A local, deterministic stand-in for a real Gemini model, implementing
google.adk.models.BaseLlm directly -- no HTTP server needed (ADK accepts a
BaseLlm instance in place of a model name string, so this plugs straight
into Agent(model=...) with zero wire-format faking).

Deterministic by keyword, not by an actual model decision: the first turn
routes to salesforce_lookup or hr_lookup based on words in the user's
message, the second turn (once a function response is present) relays
that result as the final answer. This is intentional -- a demo of Cedar
enforcement should not also depend on an LLM's tool-choice being
reproducible run to run.

Used by both example_no_governance.py and example_governed.py whenever
_use_mock() (see either file) decides no real model is configured.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types


# Canned, deterministic usage figures -- not real token counts, but present
# so example_governed.py/example_no_governance.py's token/cost reporting
# has something to show in mock mode, same as mock_model_server.py's own
# canned `usage` block for the MAF version of this demo.
_MOCK_USAGE = types.GenerateContentResponseUsageMetadata(
    prompt_token_count=1, candidates_token_count=1, total_token_count=2
)


class MockLlm(BaseLlm):
    model: str = "mock-model"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last_user_text = ""
        last_function_response = None
        for content in llm_request.contents:
            for part in content.parts or []:
                if content.role == "user" and part.text:
                    last_user_text = part.text
                if part.function_response is not None:
                    last_function_response = part.function_response

        if last_function_response is not None:
            result = last_function_response.response
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Here's what I found: {result}")],
                ),
                usage_metadata=_MOCK_USAGE,
            )
            return

        text = last_user_text.lower()
        if re.search(r"salesforce|opportunity|deal|pipeline", text):
            tool_name = "salesforce_lookup"
        elif re.search(r"\bhr\b|benefits|payroll|pto", text):
            tool_name = "hr_lookup"
        else:
            tool_name = "salesforce_lookup"

        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name=tool_name, args={"query": last_user_text}
                        )
                    )
                ],
            ),
            usage_metadata=_MOCK_USAGE,
        )
