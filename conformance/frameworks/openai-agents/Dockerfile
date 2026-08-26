# Probe: does OpenAI Agents SDK honour OPENAI_BASE_URL with zero code change?
# Pin the SDK version -- the whole point is detecting when an upgrade breaks us.
FROM python:3.12-slim
RUN pip install --no-cache-dir "openai-agents==0.*" "openai>=1.0"
COPY probe.py /probe.py
CMD ["python", "/probe.py"]
