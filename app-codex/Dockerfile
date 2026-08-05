FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ PIP_DEFAULT_TIMEOUT=120

COPY pyproject.toml ./
COPY app_codex ./app_codex
RUN pip install --no-cache-dir .

EXPOSE 8010
CMD ["uvicorn", "app_codex.main:app", "--host", "0.0.0.0", "--port", "8010"]

