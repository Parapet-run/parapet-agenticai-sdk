# Probe: does AutoGen (via openai-python, inside autogen-ext's OpenAI client)
# honour OPENAI_BASE_URL with zero code change?
# TRAP (matrix.yaml): the model client is constructed explicitly in user code,
# unlike openai-agents' implicit client -- but base_url is never passed, so
# resolution still falls through to the underlying openai-python client's own
# env var read. That is what this probe proves.
# Pin the SDK version -- the whole point is detecting when an upgrade breaks us.
FROM python:3.12-slim
RUN pip install --no-cache-dir "autogen-agentchat==0.7.*" "autogen-ext[openai]==0.7.*"
COPY probe.py /probe.py
CMD ["python", "/probe.py"]
