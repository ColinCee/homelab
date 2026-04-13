FROM python:3.14-slim

# Pin tool versions for reproducible builds and supply-chain safety.
ARG MISE_VERSION=v2026.2.1
ARG COPILOT_CLI_VERSION=v1.0.25

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
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install mise and provision all dev tools (shellcheck, actionlint, trivy)
# so the Copilot CLI has the same toolchain as local dev.
# Shared data dir with shims on PATH — tools callable by any user.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV MISE_DATA_DIR="/mise"
ENV MISE_CONFIG_DIR="/mise"
ENV MISE_CACHE_DIR="/mise/cache"
ENV MISE_INSTALL_PATH="/usr/local/bin/mise"
RUN MISE_VERSION="${MISE_VERSION}" curl https://mise.run | sh
COPY mise.toml /mise/config.toml
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
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
