"""Stdio-transport wrapper around conformance/mcp-probe/server.py's FastMCP
instance -- reused, not copied. That file hardcodes transport="streamable-http"
in its own __main__ block and must not be edited (spike constraint: no
restructuring, no file moves), so this runs the *same* tool definitions over
stdio instead by importing the mcp object directly and calling .run()
differently here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_MCP_PROBE_DIR = Path(__file__).resolve().parents[2] / "conformance" / "mcp-probe"
sys.path.insert(0, str(_MCP_PROBE_DIR))

from server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run(transport="stdio")
