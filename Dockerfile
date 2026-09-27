FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential linux-libc-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY web/ web/
COPY vendor/ vendor/
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8080
CMD ["/app/.venv/bin/uvicorn", "src.webapp:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
