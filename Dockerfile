FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

WORKDIR /app
ENV DAGSTER_HOME=/app \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY job_finder ./job_finder
COPY dagster.yaml workspace.yaml ./

EXPOSE 3000
CMD ["sh", "-c", "uv run dagster-webserver -h 0.0.0.0 -p ${PORT:-3000} -w workspace.yaml"]
