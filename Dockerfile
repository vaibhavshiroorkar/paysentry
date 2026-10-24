FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY gateway ./gateway
EXPOSE 8000
# Hosts such as Render choose the port through PORT.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
