# syntax=docker/dockerfile:1.7
# Standalone -- no dependency on parapetai-agent/parapetai-gateway. This is a
# toy MCP server, not part of the SDK; it exists only as a deployable demo
# upstream for gateway/deploy/azure/deploy-sample-mcp-server.sh.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "mcp>=1.24,<2.0"
COPY gateway/deploy/azure/sample-mcp-server/server.py ./server.py

RUN useradd -m -u 1000 mcpdemo
USER mcpdemo

EXPOSE 8090
# A bare GET to /mcp isn't guaranteed a 2xx by FastMCP's streamable-http
# transport (it expects a real MCP session, not an arbitrary HTTP client) --
# a raw TCP connect is what actually indicates "server is up and listening"
# without depending on Streamable HTTP's own request/response semantics.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8090), timeout=3).close()" || exit 1

ENTRYPOINT ["python", "server.py"]
