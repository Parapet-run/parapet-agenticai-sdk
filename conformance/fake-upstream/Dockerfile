# Canned OpenAI-shaped upstream for CI conformance runs -- no credentials,
# no live network dependency. See app.py for what it actually implements.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn>=0.30,<1.0"
COPY app.py /app.py
CMD ["uvicorn", "--app-dir", "/", "app:app", "--host", "0.0.0.0", "--port", "9001"]
