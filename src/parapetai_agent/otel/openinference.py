"""Hand-rolled subset of the OpenInference semantic-convention attribute
names (github.com/Arize-ai/openinference). Every `key` string below was
verified byte-for-byte against the real `openinference-semantic-conventions`
0.1.32 source (`openinference/semconv/trace/__init__.py`), not
reimplemented from memory -- see docs/adr/0007 for why this is a vendored
constant subset rather than a dependency on that package.

ATTRS is ADDITIVE-ONLY: growing from today's ~20-entry subset toward the
spec's full ~80 attributes is adding OpenInferenceAttr(...) entries here,
never restructuring -- every consumer (span-attribute population in
parapetai_agent/maf.py, the control-plane spans explorer's column categorization
in parapetai_control/spans.py) reads this registry generically by `key`/`category`/
`content_bearing`, with no attribute name hardcoded anywhere else.

`content_bearing=True` marks a key that can carry raw prompt/response/
document text -- CLAUDE.md invariant 10 territory. A caller setting span
attributes MUST gate every content_bearing key behind its own opt-in
(parapetai_agent/maf.py's PARAPETAI_OTEL_LOG_CONTENT); this module only classifies,
it does not enforce -- enforcement lives at the one place full content is
actually in memory (see docs/adr/0007).
"""

from __future__ import annotations

from dataclasses import dataclass

# openinference.span.kind's own attribute key. Its VALUES (SpanKind below)
# are a small closed enum, unlike every other key here -- but the key
# itself is still a real entry in ATTRS/BY_KEY like any other, so a
# consumer that only ever looks a key up by string (parapetai_control/spans.py's
# column categorization) finds it too.
SPAN_KIND_ATTR = "openinference.span.kind"


class SpanKind:
    """OpenInferenceSpanKindValues subset this integration can produce.
    The real spec also defines RETRIEVER/EMBEDDING/RERANKER/GUARDRAIL/
    EVALUATOR/PROMPT/UNKNOWN -- add here, additively, if a future span
    source can actually populate one (see docs/adr/0007's scope note: this
    codebase governs LLM/tool calls, not a RAG pipeline, so those five
    have no data source today)."""

    LLM = "LLM"
    TOOL = "TOOL"
    CHAIN = "CHAIN"
    AGENT = "AGENT"


@dataclass(frozen=True, slots=True)
class OpenInferenceAttr:
    key: str
    category: str  # "llm" | "tool" | "generic" | "session"
    content_bearing: bool


# Named constants, not bare strings at call sites -- a typo in a consumer
# (parapetai_agent/maf.py, parapetai_control/spans.py) becomes an ImportError/mypy error
# instead of silently minting an untracked attribute key. ATTRS below is
# built FROM these, so there is exactly one place each key is spelled out.
LLM_MODEL_NAME = "llm.model_name"
LLM_PROVIDER = "llm.provider"
LLM_INVOCATION_PARAMETERS = "llm.invocation_parameters"
LLM_TOOLS = "llm.tools"
LLM_TOKEN_COUNT_PROMPT = "llm.token_count.prompt"  # noqa: S105 -- not a secret, an OpenInference attribute name
LLM_TOKEN_COUNT_COMPLETION = "llm.token_count.completion"  # noqa: S105
LLM_TOKEN_COUNT_TOTAL = "llm.token_count.total"  # noqa: S105
LLM_INPUT_MESSAGES = "llm.input_messages"
LLM_OUTPUT_MESSAGES = "llm.output_messages"
TOOL_NAME = "tool.name"
TOOL_DESCRIPTION = "tool.description"
TOOL_ID = "tool.id"
TOOL_PARAMETERS = "tool.parameters"
INPUT_MIME_TYPE = "input.mime_type"
OUTPUT_MIME_TYPE = "output.mime_type"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
SESSION_ID = "session.id"
METADATA = "metadata"

ATTRS: tuple[OpenInferenceAttr, ...] = (
    # Span kind -- metadata, a small closed enum (SpanKind above).
    OpenInferenceAttr(SPAN_KIND_ATTR, "generic", False),
    # LLM span (parapetai.model_call) -- metadata.
    OpenInferenceAttr(LLM_MODEL_NAME, "llm", False),
    OpenInferenceAttr(LLM_PROVIDER, "llm", False),
    OpenInferenceAttr(LLM_INVOCATION_PARAMETERS, "llm", False),
    OpenInferenceAttr(LLM_TOOLS, "llm", False),
    OpenInferenceAttr(LLM_TOKEN_COUNT_PROMPT, "llm", False),
    OpenInferenceAttr(LLM_TOKEN_COUNT_COMPLETION, "llm", False),
    OpenInferenceAttr(LLM_TOKEN_COUNT_TOTAL, "llm", False),
    # LLM span -- content-bearing (full message text).
    OpenInferenceAttr(LLM_INPUT_MESSAGES, "llm", True),
    OpenInferenceAttr(LLM_OUTPUT_MESSAGES, "llm", True),
    # Tool span (parapetai.tool_call) -- metadata.
    OpenInferenceAttr(TOOL_NAME, "tool", False),
    OpenInferenceAttr(TOOL_DESCRIPTION, "tool", False),
    OpenInferenceAttr(TOOL_ID, "tool", False),
    # Tool span -- content-bearing (real argument/result values).
    OpenInferenceAttr(TOOL_PARAMETERS, "tool", True),
    # Generic input/output -- metadata (mime type) vs. content (the value).
    OpenInferenceAttr(INPUT_MIME_TYPE, "generic", False),
    OpenInferenceAttr(OUTPUT_MIME_TYPE, "generic", False),
    OpenInferenceAttr(INPUT_VALUE, "generic", True),
    OpenInferenceAttr(OUTPUT_VALUE, "generic", True),
    # Session/generic -- metadata.
    OpenInferenceAttr(SESSION_ID, "session", False),
    OpenInferenceAttr(METADATA, "generic", False),
)

BY_KEY: dict[str, OpenInferenceAttr] = {a.key: a for a in ATTRS}


def content_bearing_keys() -> frozenset[str]:
    return frozenset(a.key for a in ATTRS if a.content_bearing)
