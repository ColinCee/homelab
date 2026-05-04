# renovate: datasource=docker depName=python
FROM python:3.14.4-slim@sha256:c11aee3b3cae066f55d1e9318fc812673aa6557073b0db0d792b59491b262e0c

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/knowledge/ knowledge/
COPY migrations/ migrations/

RUN adduser --system --no-create-home app && \
    mkdir -p /home/user/.ssh && chmod 700 /home/user/.ssh && \
    ssh-keyscan github.com > /home/user/.ssh/known_hosts 2>/dev/null && \
    chown -R 1000:1000 /home/user/.ssh
USER app

ENTRYPOINT [".venv/bin/python", "-m", "knowledge"]
