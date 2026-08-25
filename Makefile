.PHONY: install test lint typecheck check conformance

install:
	pip install -e ".[maf,web,dev]"

test:
	pytest -q

lint:
	ruff check src tests

typecheck:
	mypy src

check: lint typecheck test

# The end-to-end MCP/live tests spawn fixture servers via `uv run` (see
# conformance/README.md); they skip cleanly if uv is unavailable.
conformance:
	pytest -q tests/test_maf.py -k "ToolSourcesLiveEndToEnd or GovernedAgent"
