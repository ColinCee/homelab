FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Copilot CLI standalone binary
RUN curl -fsSL https://github.com/github/copilot-cli/releases/latest/download/copilot-linux-x64.tar.gz \
    | tar -xz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/copilot

# Install gh CLI for GitHub API access
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Non-root user for security
RUN useradd -m -s /bin/bash agent
USER agent

ENV HOME=/home/agent
ENV PATH="/app/.venv/bin:$PATH"

# Squid proxy for egress filtering (set at runtime via compose)
# ENV HTTPS_PROXY=http://proxy:3128
# ENV HTTP_PROXY=http://proxy:3128

# Copilot CLI authenticates via COPILOT_GITHUB_TOKEN env var (set in compose)

EXPOSE 8585
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
