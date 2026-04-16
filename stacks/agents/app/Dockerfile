FROM python:3.14.4-slim

# Pin tool versions for reproducible builds and supply-chain safety.
# Renovate tracks each ARG via the datasource comments below.
# When bumping Python, also update: mise.toml (python), pyproject.toml (requires-python, target-version) x2.
# renovate: datasource=github-releases depName=jdx/mise
ARG MISE_VERSION=v2026.2.1
# renovate: datasource=github-releases depName=github/copilot-cli
ARG COPILOT_CLI_VERSION=v1.0.30
# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=2.89.0
# renovate: datasource=github-releases depName=moby/moby
ARG DOCKER_VERSION=28.2.2
# renovate: datasource=github-releases depName=docker/compose
ARG DOCKER_COMPOSE_VERSION=5.1.3

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Copilot CLI standalone binary
RUN curl -fsSL "https://github.com/github/copilot-cli/releases/download/${COPILOT_CLI_VERSION}/copilot-linux-x64.tar.gz" \
    | tar -xz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/copilot

# Install gh CLI for GitHub API access
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar -xz --strip-components=2 -C /usr/local/bin "gh_${GH_VERSION}_linux_amd64/bin/gh"

# Install Docker CLI for spawning ephemeral worker containers (ADR-011)
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

# Install Docker Compose CLI plugin so workers can run `docker compose config`
RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Install mise and provision all dev tools (shellcheck, actionlint, trivy)
# so the Copilot CLI has the same toolchain as local dev.
# Shared data dir with shims on PATH — tools callable by any user.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV MISE_DATA_DIR="/mise"
ENV MISE_CONFIG_DIR="/mise"
ENV MISE_CACHE_DIR="/mise/cache"
ENV MISE_INSTALL_PATH="/usr/local/bin/mise"
RUN MISE_VERSION="${MISE_VERSION}" curl https://mise.run | sh
COPY stacks/agents/app/mise.docker.toml /mise/config.toml
RUN mise install --yes && mise reshim

WORKDIR /app
COPY stacks/agents/app/pyproject.toml stacks/agents/app/uv.lock ./
RUN uv sync --frozen

COPY stacks/agents/app/ .

# Non-root user for security
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /repo.git /reviews && \
    chown agent:agent /repo.git /reviews /mise

COPY stacks/agents/app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/home/agent
ENV PATH="/mise/shims:/app/.venv/bin:$PATH"

# Copilot CLI authenticates via COPILOT_GITHUB_TOKEN env var (set in compose)

EXPOSE 8585
ENTRYPOINT ["/entrypoint.sh"]
# API mode (default) — override with 'python -m worker' for ephemeral workers
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
