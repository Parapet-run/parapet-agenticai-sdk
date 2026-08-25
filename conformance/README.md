# conformance/ — hermetic test fixtures

These are servers the test suite spawns; they are **not** part of the shipped
package.

- `fake-upstream/app.py` — a canned OpenAI-shaped model server. Stands in for a
  real provider so the end-to-end middleware tests run with no credentials, no
  network egress, and no model non-determinism.
- `mcp-probe/server.py` — a minimal MCP server (streamable-http) exposing one
  tool, used to exercise governance over MCP tool sources.
- `mcp-probe/stdio_server.py` — the same MCP tool definitions over stdio.
- `mcp-probe/client_probe.py` — a small MCP client used by the probes.

The test fixtures launch these via `uv run --no-project` (fastapi/uvicorn/mcp),
so they need `uv` on PATH; a fixture whose server can't start skips rather than
fails.
