"""The gateway must keep working against a parapetai-agent that predates the
heartbeat `details` block.

The gateway image installs parapetai-agent from a package index, which can lag
this gateway. If the gateway passed `details_provider` to a poller that does not
accept it, the poller thread would die with a TypeError: no more policy refresh,
no more heartbeats. That is the failure this pins against.
"""

from __future__ import annotations

from typing import Any

import parapetai_gateway.server.main as main_module
import pytest
from parapetai_gateway.status import GatewayStatus


def _poller_with(*extra_params: str) -> Any:
    params = ", ".join(("url", "secret", "policy_dir", *extra_params))
    namespace: dict[str, Any] = {}
    exec(f"def run_bundle_poller({params}, **kw): ...", namespace)  # noqa: S102 -- test-built signature
    return namespace["run_bundle_poller"]


def test_the_status_block_is_passed_when_the_installed_sdk_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "run_bundle_poller", _poller_with("details_provider"))
    status = GatewayStatus()

    assert main_module._details_kwargs(status) == {"details_provider": status.snapshot}


def test_an_older_sdk_gets_no_details_argument_instead_of_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "run_bundle_poller", _poller_with("on_bundle_meta"))

    assert main_module._details_kwargs(GatewayStatus()) == {}


def test_the_omission_is_logged_so_an_operator_can_see_why_the_page_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "run_bundle_poller", _poller_with())

    main_module._details_kwargs(GatewayStatus())

    assert "heartbeat_details_unsupported_by_installed_sdk" in capsys.readouterr().out


def test_the_installed_sdk_in_this_workspace_supports_it() -> None:
    """Guards the other direction: if the real signature loses the argument, the
    feature silently turns off, and this fails instead."""
    assert main_module._details_kwargs(GatewayStatus()) != {}
