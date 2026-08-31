# syntax=docker/dockerfile:1.7
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY gateway/pyproject.toml gateway/README.md ./
COPY gateway/src ./src
RUN pip install --no-cache-dir . \
    && python -c "import cedarpy; print('cedarpy OK')"

RUN useradd -m -u 1000 parapetai && mkdir -p /etc/parapetai/policies && chown -R parapetai /etc/parapetai
USER parapetai

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/__parapetai/ready')" || exit 1

ENTRYPOINT ["python", "-m", "parapetai_gateway.server.main"]
