# Probe: does CrewAI honour OPENAI_API_BASE with zero code change?
# NOTE: CrewAI uses native provider SDKs, not LiteLLM, for OpenAI -- this probe
# only proves the OpenAI path. See CLAUDE.md / matrix.yaml TRAP note: the
# Anthropic/Gemini/Azure/Bedrock paths need their own separate probes.
# Pin the SDK version -- the whole point is detecting when an upgrade breaks us.
FROM python:3.12-slim
RUN pip install --no-cache-dir "crewai==1.*"
COPY probe.py /probe.py
CMD ["python", "/probe.py"]
