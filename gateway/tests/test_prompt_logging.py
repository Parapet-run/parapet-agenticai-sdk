"""Opt-in prompt content logging (PARAPETAI_LOG_PROMPTS, default off). See
docs/adr/0005. This is a deliberately separate surface from the "decision"
audit log and /__parapetai/observations, both of which never carry content
regardless of this setting -- these tests pin that separation down.

Uses capsys, not caplog: structlog's default logger_factory (PrintLogger,
unconfigured in tests -- structlog.configure() is only called in
server/main.py's production entrypoint) writes straight to stdout, not
through stdlib logging, so caplog captures nothing here. Confirmed the hard
way: an earlier caplog-based version of this file had its "logged when
enabled" tests fail (nothing captured) while its "not logged by default"
test passed -- vacuously, for the wrong reason, since caplog saw nothing
either way regardless of what the code did.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import parapetai_gateway.server.app as app_module
import pytest
from fastapi.testclient import TestClient
from parapetai_gateway.server.app import create_app

from parapetai_agent.policy.engine import PolicyEngine

POLICIES = Path(__file__).resolve().parents[2] / "policies"

SENSITIVE_PROMPT = "My order 12345 shipped but the card ending 4242 was charged twice"


@pytest.fixture
def client() -> TestClient:
    engine = PolicyEngine(POLICIES, POLICIES / "entities.json")
    return TestClient(create_app(engine))


def _post_prompt(client: TestClient, agent_id: str, content: str, **headers: str) -> None:
    client.post(
        f"/a/{agent_id}/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": content}]},
        headers=headers,
    )


def test_prompt_content_not_logged_by_default(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert app_module.settings.log_prompts is False  # the default this test relies on

    _post_prompt(client, "no-log-agent", SENSITIVE_PROMPT)

    out = capsys.readouterr().out
    assert "prompt_content" not in out
    assert SENSITIVE_PROMPT not in out


def test_prompt_content_logged_when_opted_in(
    client: TestClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, log_prompts=True)
    )

    _post_prompt(client, "log-agent", SENSITIVE_PROMPT)

    out = capsys.readouterr().out
    assert "prompt_content" in out
    assert SENSITIVE_PROMPT in out


def test_decision_log_stays_content_free_even_when_prompt_logging_is_on(
    client: TestClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing/decision audit trail must stay content-free regardless of
    PARAPETAI_LOG_PROMPTS -- prompt content lives only in the separate
    "prompt_content" event, never folded into "decision"."""
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, log_prompts=True)
    )

    _post_prompt(client, "separation-check", SENSITIVE_PROMPT)

    out = capsys.readouterr().out
    decision_lines = [line for line in out.splitlines() if " decision " in line]
    assert decision_lines
    for line in decision_lines:
        assert SENSITIVE_PROMPT not in line
        assert "messages_preview" not in line


def test_prompt_content_log_never_includes_a_credential(
    client: TestClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, log_prompts=True)
    )

    _post_prompt(client, "cred-check", SENSITIVE_PROMPT, Authorization="Bearer super-secret-value")

    out = capsys.readouterr().out
    assert "super-secret-value" not in out
