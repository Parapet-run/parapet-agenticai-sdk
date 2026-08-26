# Probe: does LangGraph (via langchain-openai) honour OPENAI_API_BASE /
# OPENAI_BASE_URL with zero code change?
# Pin the SDK version -- the whole point is detecting when an upgrade breaks us.
FROM python:3.12-slim
RUN pip install --no-cache-dir "langgraph==1.*" "langchain-openai==1.*"
COPY probe.py /probe.py
CMD ["python", "/probe.py"]
