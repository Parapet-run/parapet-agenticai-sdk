"""parapetai_agent.otel.openinference -- the hand-rolled, additive OpenInference
attribute-name registry. Verifies the structural contract (no duplicate
keys) and spot-checks content_bearing classification against the real
openinference-semantic-conventions 0.1.32 values this module was verified
against (see its own module docstring)."""

from __future__ import annotations

from parapetai_agent.otel.openinference import (
    ATTRS,
    BY_KEY,
    SPAN_KIND_ATTR,
    SpanKind,
    content_bearing_keys,
)


def test_no_duplicate_keys() -> None:
    keys = [a.key for a in ATTRS]
    assert len(keys) == len(set(keys))


def test_by_key_matches_attrs() -> None:
    assert set(BY_KEY) == {a.key for a in ATTRS}
    for key, attr in BY_KEY.items():
        assert attr.key == key


def test_content_bearing_classification_matches_verified_spec() -> None:
    assert BY_KEY["llm.input_messages"].content_bearing is True
    assert BY_KEY["llm.output_messages"].content_bearing is True
    assert BY_KEY["tool.parameters"].content_bearing is True
    assert BY_KEY["input.value"].content_bearing is True
    assert BY_KEY["output.value"].content_bearing is True

    assert BY_KEY["llm.model_name"].content_bearing is False
    assert BY_KEY["llm.provider"].content_bearing is False
    assert BY_KEY["llm.token_count.prompt"].content_bearing is False
    assert BY_KEY["tool.name"].content_bearing is False
    assert BY_KEY["session.id"].content_bearing is False


def test_content_bearing_keys_helper_matches_flags() -> None:
    assert content_bearing_keys() == {a.key for a in ATTRS if a.content_bearing}


def test_span_kind_values_are_strings_the_registry_expects() -> None:
    assert SpanKind.LLM == "LLM"
    assert SpanKind.TOOL == "TOOL"


def test_span_kind_attr_itself_is_a_registered_key() -> None:
    # Regression: this key was originally kept out of ATTRS (only its
    # closed value set lived in SpanKind), which meant BY_KEY.get() on it
    # silently returned None everywhere -- a consumer keying off BY_KEY
    # (parapetai_control/spans.py's column categorization) treated it as an unknown
    # attribute and defaulted it to hidden regardless of any curated
    # visible-set. It must be a real ATTRS/BY_KEY entry like any other key.
    assert SPAN_KIND_ATTR in BY_KEY
    assert BY_KEY[SPAN_KIND_ATTR].content_bearing is False
