# renovate: datasource=docker depName=python
FROM python:3.14.4-slim@sha256:538a18f1db92b4210a0b71aca2d14c156a96dedbe8867465c8ff4dce04d2ec39

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY knowledge/ knowledge/

RUN adduser --system --no-create-home app
USER app

ENTRYPOINT ["uv", "run", "python", "-m", "knowledge"]
