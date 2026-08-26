.PHONY: install test test-sdk test-gateway lint typecheck check conformance

# This repo is a uv workspace: the root package (parapetai-agent, the
# in-process PEP) plus two members -- gateway/ (the proxy PEP, same Cedar
# engine, for apps that can't embed) and mcp-server/ (parapetai-mcp).
# Members are NOT installed by a root `uv sync`, so their suites run under
# `uv run --package <name>`; running them from the root venv fails with
# ModuleNotFoundError: parapetai_gateway.

install:
	uv sync --all-extras

test: test-sdk test-gateway

test-sdk:
	uv run --extra maf --extra judge --extra dev pytest tests -q

test-gateway:
	uv run --package parapetai-gateway --extra dev pytest gateway/tests -q

lint:
	uv run --extra dev ruff check src tests gateway/src gateway/tests
	uv run --extra dev ruff format --check src gateway/src

typecheck:
	uv run --extra dev mypy src
	uv run --package parapetai-gateway --extra dev mypy gateway/src

check: lint typecheck test

# The end-to-end MCP/live tests spawn fixture servers via `uv run` (see
# conformance/README.md); they skip cleanly if uv is unavailable.
conformance:
	uv run --extra maf --extra dev pytest -q tests/test_maf.py -k "ToolSourcesLiveEndToEnd or GovernedAgent"
