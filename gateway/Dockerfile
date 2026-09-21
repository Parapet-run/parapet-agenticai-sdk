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

# 8080: plain HTTP (the default). 8443: the agent-facing listener when mTLS is on
# (PARAPETAI_PORT=8443). 8081: the plain-HTTP health listener (PARAPETAI_HEALTH_PORT)
# -- for an orchestrator's probes only; never publish it.
EXPOSE 8080 8443 8081
# Probes the health listener when there is one, else the main port. Under mTLS the
# main port demands a client certificate, which a health check does not carry.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u;p=os.environ.get('PARAPETAI_HEALTH_PORT') or os.environ.get('PARAPETAI_PORT','8080');u.urlopen('http://127.0.0.1:%s/__parapetai/ready'%p)" || exit 1

ENTRYPOINT ["python", "-m", "parapetai_gateway.server.main"]
