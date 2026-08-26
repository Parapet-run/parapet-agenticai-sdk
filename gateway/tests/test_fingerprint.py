"""Client fingerprint derivation. UA strings below are real, captured from
each SDK's actual outbound request via a local header-echo server -- not
copied from vendor docs. See parapetai_gateway.fingerprint for the derivation table."""

from __future__ import annotations

import pytest
from parapetai_gateway.fingerprint import UNKNOWN, fingerprint

# Representative full header sets, as a real request would carry them --
# including credential headers, so the security assertions below are
# meaningful rather than vacuous.
OPENAI_PYTHON_HEADERS = {
    "host": "gateway:8080",
    "authorization": "Bearer sk-real-secret-value",
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": "OpenAI/Python 2.52.0",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "2.52.0",
    "x-stainless-os": "Linux",
    "x-stainless-arch": "arm64",
    "x-stainless-runtime": "CPython",
    "x-stainless-runtime-version": "3.12.13",
    "x-stainless-async": "false",
}

ASYNC_OPENAI_PYTHON_HEADERS = {
    "host": "gateway:8080",
    "authorization": "Bearer sk-real-secret-value",
    "content-type": "application/json",
    "user-agent": "AsyncOpenAI/Python 2.52.0",
    "x-stainless-package-version": "2.52.0",
    "x-stainless-async": "async:asyncio",
}

# openai-agents wraps an AsyncOpenAI client internally, so x-stainless-* still
# reflects the *openai* package's version (2.52.0), not agents' own (0.19.2).
# The regression this guards: naively reusing version_header here would
# silently record the wrong package's version.
OPENAI_AGENTS_HEADERS = {
    "host": "gateway:8080",
    "authorization": "Bearer sk-real-secret-value",
    "content-type": "application/json",
    "user-agent": "Agents/Python 0.19.2",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "2.52.0",
    "x-stainless-async": "async:asyncio",
}

ANTHROPIC_PYTHON_HEADERS = {
    "host": "gateway:8080",
    "x-api-key": "sk-ant-real-secret-value",
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": "Anthropic/Python 0.120.2",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "0.120.2",
    "x-stainless-os": "Linux",
    "x-stainless-arch": "arm64",
    "anthropic-version": "2023-06-01",
}

GOOGLE_GENAI_HEADERS = {
    "host": "gateway:8080",
    "x-goog-api-key": "real-secret-value",
    "content-type": "application/json",
    "user-agent": "google-genai-sdk/2.16.0 gl-python/3.12.13",
    "x-goog-api-client": "google-genai-sdk/2.16.0 gl-python/3.12.13",
}

LITELLM_HEADERS = {
    "host": "gateway:8080",
    "authorization": "Bearer sk-real-secret-value",
    "content-type": "application/json",
    "user-agent": "litellm/1.55.3",
}


@pytest.mark.parametrize(
    "headers,expected_name,expected_version",
    [
        (OPENAI_PYTHON_HEADERS, "openai-python", "2.52.0"),
        (ASYNC_OPENAI_PYTHON_HEADERS, "openai-python", "2.52.0"),
        (OPENAI_AGENTS_HEADERS, "openai-agents", "0.19.2"),
        (ANTHROPIC_PYTHON_HEADERS, "anthropic-python", "0.120.2"),
        (GOOGLE_GENAI_HEADERS, "google-genai", "2.16.0"),
        (LITELLM_HEADERS, "litellm", "1.55.3"),
    ],
)
def test_known_shapes(headers: dict[str, str], expected_name: str, expected_version: str) -> None:
    assert fingerprint(headers) == (expected_name, expected_version)


def test_openai_agents_does_not_borrow_the_inner_openai_clients_version() -> None:
    # The UA says agents 0.19.2; x-stainless-package-version says openai
    # 2.52.0. Must report the agents version, not the openai one.
    name, version = fingerprint(OPENAI_AGENTS_HEADERS)
    assert name == "openai-agents"
    assert version == "0.19.2"
    assert version != OPENAI_AGENTS_HEADERS["x-stainless-package-version"]


def test_openai_version_header_takes_priority_over_ua_parsed_version() -> None:
    headers = dict(OPENAI_PYTHON_HEADERS, **{"x-stainless-package-version": "9.9.9-patched"})
    assert fingerprint(headers) == ("openai-python", "9.9.9-patched")


def test_openai_falls_back_to_ua_version_when_header_absent() -> None:
    headers = {k: v for k, v in OPENAI_PYTHON_HEADERS.items() if k != "x-stainless-package-version"}
    assert fingerprint(headers) == ("openai-python", "2.52.0")


def test_unrecognised_user_agent_is_recorded_raw_not_dropped() -> None:
    name, version = fingerprint({"user-agent": "curl/8.7.1"})
    assert name == UNKNOWN
    assert version == "curl/8.7.1"


def test_missing_user_agent_is_unknown_with_no_version() -> None:
    assert fingerprint({}) == (UNKNOWN, None)


@pytest.mark.parametrize(
    "headers",
    [
        OPENAI_PYTHON_HEADERS,
        ASYNC_OPENAI_PYTHON_HEADERS,
        OPENAI_AGENTS_HEADERS,
        ANTHROPIC_PYTHON_HEADERS,
        GOOGLE_GENAI_HEADERS,
        LITELLM_HEADERS,
    ],
)
def test_never_derives_a_value_containing_a_credential(headers: dict[str, str]) -> None:
    name, version = fingerprint(headers)
    combined = f"{name} {version}".lower()
    assert "authorization" not in combined
    assert "api-key" not in combined
    assert "bearer" not in combined
