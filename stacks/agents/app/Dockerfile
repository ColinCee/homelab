FROM python:3.14.4-slim

# Pin tool versions for reproducible builds and supply-chain safety.
# Renovate tracks each ARG via the datasource comments below.
# When bumping Python, also update: mise.toml (python), pyproject.toml (requires-python, target-version) x2.
# renovate: datasource=github-releases depName=jdx/mise
ARG MISE_VERSION=v2026.2.1
# renovate: datasource=github-releases depName=github/copilot-cli
ARG COPILOT_CLI_VERSION=v1.0.30
# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=2.90.0
# renovate: datasource=github-releases depName=moby/moby
ARG DOCKER_VERSION=28.2.2
# renovate: datasource=github-releases depName=docker/compose
ARG DOCKER_COMPOSE_VERSION=5.1.3

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates gettext-base && \
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

# Install mise and provision all dev tools so the Copilot CLI has the same
# toolchain as local dev. mise.toml is the single source of truth for versions.
# Shared data dir with shims on PATH — tools callable by any user.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV MISE_DATA_DIR="/mise"
ENV MISE_CONFIG_DIR="/mise"
ENV MISE_CACHE_DIR="/mise/cache"
ENV MISE_INSTALL_PATH="/usr/local/bin/mise"
RUN MISE_VERSION="${MISE_VERSION}" curl https://mise.run | sh
COPY mise.toml /mise/config.toml

# Register base-image Python with mise so it skips compilation.
# mise detects installed tools by scanning /mise/installs/<tool>/<version>/.
ARG PYTHON_VERSION=3.14.4
RUN mkdir -p /mise/installs/python/${PYTHON_VERSION}/bin && \
    ln -sf /usr/local/bin/python3 /mise/installs/python/${PYTHON_VERSION}/bin/python && \
    ln -sf /usr/local/bin/python3 /mise/installs/python/${PYTHON_VERSION}/bin/python3 && \
    ln -sf /usr/local/bin/pip3 /mise/installs/python/${PYTHON_VERSION}/bin/pip 2>/dev/null || true

# Register COPY-installed uv with mise (version detected dynamically).
RUN UV_VERSION=$(uv --version | awk '{print $2}') && \
    mkdir -p "/mise/installs/uv/${UV_VERSION}/bin" && \
    ln -sf /usr/local/bin/uv "/mise/installs/uv/${UV_VERSION}/bin/uv"

# Install remaining tools (shellcheck, actionlint, trivy) — Python and uv are
# already registered above so mise skips them.
RUN mise install --yes && mise reshim

# Verify all mise-managed tools are installed. Fails the build if anything is
# missing — catches version drift between mise.toml and the Dockerfile before
# the image ships.
RUN mise ls --json | python3 -c "\
import json, sys; tools = json.load(sys.stdin); \
missing = [t for t in tools if not tools[t][0].get('installed')]; \
sys.exit(f'Missing tools: {missing}') if missing else print('All mise tools verified ✓')"
# Verify non-mise tools and that shims resolve correctly.
RUN python3 --version && uv --version && envsubst --version && \
    /mise/shims/shellcheck --version | head -1 && /mise/shims/actionlint --version

WORKDIR /app
COPY stacks/agents/app/pyproject.toml stacks/agents/app/uv.lock ./
RUN uv sync --frozen

# Pre-warm uv cache with root dev deps (yamllint, ruff, ty, pytest) so
# runtime `uv sync` in worktrees hits cache instead of downloading.
COPY pyproject.toml uv.lock /tmp/repo-root/
RUN cd /tmp/repo-root && uv sync --frozen && rm -rf /tmp/repo-root

COPY stacks/agents/app/ .

# Non-root user for security
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /repo.git /reviews && \
    chown agent:agent /repo.git /reviews /mise

COPY stacks/agents/app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/home/agent
ENV PATH="/app/.venv/bin:/mise/shims:$PATH"
# Trust mise configs in worktrees (agent clones repos into /reviews/).
ENV MISE_TRUSTED_CONFIG_PATHS="/reviews"

# Copilot CLI authenticates via COPILOT_GITHUB_TOKEN env var (set in compose)

EXPOSE 8585
ENTRYPOINT ["/entrypoint.sh"]
# API mode (default) — override with 'python -m worker' for ephemeral workers
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
