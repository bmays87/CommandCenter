# Phase 0: server-only image. Dashboard build stage joins in Phase 1.
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PRODEO_DATA_DIR=/data
VOLUME /data
CMD ["uv", "run", "--no-sync", "prodeo-server"]
