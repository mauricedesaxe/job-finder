FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

WORKDIR /app
ENV DAGSTER_HOME=/app \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY job_finder ./job_finder
COPY scripts ./scripts
COPY dagster.yaml workspace.yaml ./

EXPOSE 8080
CMD ["sh", "-c", "uv run --no-sync uvicorn scripts.serve_review:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
