# Root filesystem for the CI microVMs, built with Docker and exported to ext4 by
# build-rootfs.sh. Boots Ubuntu's own kernel with systemd as init. No SSH server,
# no host keys, no credentials: each VM learns its job from Firecracker's MMDS.
# The tools jobs need are baked in (pinned in versions.env) and package caches
# come from the repo's cache disk at /cache.
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv udev iproute2 ca-certificates curl jq netcat-openbsd iputils-ping \
      git sudo unzip xz-utils zstd build-essential shellcheck file \
      kmod iptables bubblewrap socat \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-resolved.service systemd-networkd.service \
      systemd-timesyncd.service apt-daily.timer apt-daily-upgrade.timer \
      e2scrub_reap.service getty@tty1.service serial-getty@ttyS0.service \
 && rm -f /etc/machine-id /var/lib/dbus/machine-id && : > /etc/machine-id \
 && useradd --create-home --shell /bin/bash runner \
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
# Root inside the VM is fine: the VM is the boundary, and it is thrown away.
# One Python, PYTHON_VERSION (versions.env), as python and python3: Ubuntu's older python3 is not installed, so no
# job runs on a Python its repo never tested.

ARG RUNNER_VERSION
ARG RUNNER_SHA256
RUN v=${RUNNER_VERSION#v} \
 && curl -fsSL -o /tmp/runner.tgz \
      "https://github.com/actions/runner/releases/download/${RUNNER_VERSION}/actions-runner-linux-x64-${v}.tar.gz" \
 && echo "${RUNNER_SHA256}  /tmp/runner.tgz" | sha256sum -c --quiet \
 && mkdir /opt/runner && tar -xzf /tmp/runner.tgz -C /opt/runner && rm /tmp/runner.tgz \
 && /opt/runner/bin/installdependencies.sh >/dev/null \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && chown -R runner:runner /opt/runner

# The system libraries Playwright's Chromium needs on Ubuntu 24.04 (the tools and
# chromium lists `playwright install-deps chromium` installs), so a job only runs
# `playwright install chromium` to get the browser itself.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb fonts-noto-color-emoji fonts-unifont libfontconfig1 libfreetype6 \
      xfonts-cyrillic xfonts-scalable fonts-liberation fonts-ipafont-gothic \
      fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 \
      libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 \
      libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
      libxkbcommon0 libxrandr2 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# Tools jobs would otherwise download every time. Node goes where setup-node
# looks first (the runner's tool cache); Python where uv looks for it.
ARG NODE_VERSION
ARG NODE_SHA256
ARG UV_VERSION
ARG UV_SHA256
ARG PYTHON_VERSION
RUN v=${NODE_VERSION#v} \
 && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v$v/node-v$v-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c --quiet \
 && mkdir -p "/opt/hostedtoolcache/node/$v/x64" \
 && tar -xJf /tmp/node.tar.xz --strip-components 1 -C "/opt/hostedtoolcache/node/$v/x64" \
 && touch "/opt/hostedtoolcache/node/$v/x64.complete" && rm /tmp/node.tar.xz \
 && curl -fsSL -o /tmp/uv.tgz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA256}  /tmp/uv.tgz" | sha256sum -c --quiet \
 && tar -xzf /tmp/uv.tgz --strip-components 1 -C /usr/local/bin && rm /tmp/uv.tgz \
 && mkdir -p "/opt/hostedtoolcache/uv/${UV_VERSION}/x86_64" \
 && cp /usr/local/bin/uv /usr/local/bin/uvx "/opt/hostedtoolcache/uv/${UV_VERSION}/x86_64/" \
 && touch "/opt/hostedtoolcache/uv/${UV_VERSION}/x86_64.complete" \
 && UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_BIN_DIR=/usr/local/bin uv python install --default "${PYTHON_VERSION}" \
 && python3 --version | grep -q "^Python ${PYTHON_VERSION}" && python --version | grep -q "^Python ${PYTHON_VERSION}" \
 && chown -R runner:runner /opt/hostedtoolcache /opt/uv-python \
 && printf 'RUNNER_TOOL_CACHE=/opt/hostedtoolcache\nUV_PYTHON_INSTALL_DIR=/opt/uv-python\n' >> /opt/runner/.env

# A JDK (Eclipse Temurin) for Java and Gradle jobs, at JAVA_HOME=/opt/jdk.
ARG JDK_VERSION
ARG JDK_SHA256
RUN v=${JDK_VERSION} \
 && curl -fsSL -o /tmp/jdk.tgz \
      "https://github.com/adoptium/temurin${v%%[.+]*}-binaries/releases/download/jdk-$(echo "$v" | sed 's/+/%2B/')/OpenJDK${v%%[.+]*}U-jdk_x64_linux_hotspot_$(echo "$v" | tr + _).tar.gz" \
 && echo "${JDK_SHA256}  /tmp/jdk.tgz" | sha256sum -c --quiet \
 && mkdir /opt/jdk && tar -xzf /tmp/jdk.tgz --strip-components 1 -C /opt/jdk && rm /tmp/jdk.tgz \
 && echo 'JAVA_HOME=/opt/jdk' >> /opt/runner/.env

# mise (it installs the tools a repo's mise.toml lists), bun, pnpm, the GitHub
# CLI, gitleaks, and Docker with its buildx and compose plugins, all under
# /usr/local, already on the jobs' PATH. The engine starts on a job's first
# `docker` call (docker.socket), so jobs that never call it pay nothing; the
# runner user may call it without sudo. Its kernel modules (OverlayFS, bridge,
# netfilter) are the host's, copied in by build-rootfs.sh with the kernel.
ARG MISE_VERSION
ARG MISE_SHA256
ARG BUN_VERSION
ARG BUN_SHA256
ARG PNPM_VERSION
ARG PNPM_SHA256
ARG DOCKER_VERSION
ARG DOCKER_SHA256
ARG COMPOSE_VERSION
ARG COMPOSE_SHA256
ARG BUILDX_VERSION
ARG BUILDX_SHA256
ARG GH_VERSION
ARG GH_SHA256
ARG GITLEAKS_VERSION
ARG GITLEAKS_SHA256
RUN curl -fsSL -o /usr/local/bin/mise \
      "https://github.com/jdx/mise/releases/download/${MISE_VERSION}/mise-${MISE_VERSION}-linux-x64" \
 && echo "${MISE_SHA256}  /usr/local/bin/mise" | sha256sum -c --quiet \
 && curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
 && echo "${BUN_SHA256}  /tmp/bun.zip" | sha256sum -c --quiet \
 && unzip -qj /tmp/bun.zip bun-linux-x64/bun -d /usr/local/bin && rm /tmp/bun.zip \
 && curl -fsSL -o /tmp/pnpm.tgz "https://github.com/pnpm/pnpm/releases/download/v${PNPM_VERSION}/pnpm-linux-x64.tar.gz" \
 && echo "${PNPM_SHA256}  /tmp/pnpm.tgz" | sha256sum -c --quiet \
 && mkdir /usr/local/lib/pnpm && tar -xzf /tmp/pnpm.tgz -C /usr/local/lib/pnpm && rm /tmp/pnpm.tgz \
 && ln -s /usr/local/lib/pnpm/pnpm /usr/local/bin/pnpm \
 && curl -fsSL -o /tmp/docker.tgz "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION#v}.tgz" \
 && echo "${DOCKER_SHA256}  /tmp/docker.tgz" | sha256sum -c --quiet \
 && tar -xzf /tmp/docker.tgz --strip-components 1 -C /usr/local/bin && rm /tmp/docker.tgz \
 && mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
 && echo "${COMPOSE_SHA256}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c --quiet \
 && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
      "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64" \
 && echo "${BUILDX_SHA256}  /usr/local/lib/docker/cli-plugins/docker-buildx" | sha256sum -c --quiet \
 && v=${GH_VERSION#v} \
 && curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v$v/gh_${v}_linux_amd64.tar.gz" \
 && echo "${GH_SHA256}  /tmp/gh.tgz" | sha256sum -c --quiet \
 && tar -xzf /tmp/gh.tgz --strip-components 2 -C /usr/local/bin "gh_${v}_linux_amd64/bin/gh" && rm /tmp/gh.tgz \
 && v=${GITLEAKS_VERSION#v} \
 && curl -fsSL -o /tmp/gitleaks.tgz \
      "https://github.com/gitleaks/gitleaks/releases/download/v$v/gitleaks_${v}_linux_x64.tar.gz" \
 && echo "${GITLEAKS_SHA256}  /tmp/gitleaks.tgz" | sha256sum -c --quiet \
 && tar -xzf /tmp/gitleaks.tgz -C /usr/local/bin gitleaks && rm /tmp/gitleaks.tgz \
 && chmod 0755 /usr/local/bin/mise /usr/local/bin/bun /usr/local/bin/gh /usr/local/bin/gitleaks \
      /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-buildx \
 && groupadd --system docker && usermod -aG docker runner
COPY docker.socket docker.service /etc/systemd/system/
RUN systemctl enable docker.socket

# Actions the CI repos use, unpacked where the runner looks before downloading
# (<owner>_<repo>/<commit>/<the archive's own folder>), and Node and the JDK on the
# jobs' PATH. The runner's user owns the actions: one like claude-code-action installs its
# packages into its own folder when it runs, and root-owned copies failed every agent job
# (5 Nov). Each VM is thrown away after one job, so nothing a job changes there carries over.
ARG CACHED_ACTIONS
RUN for a in ${CACHED_ACTIONS}; do \
      repo=${a%@*}; sha=${a#*@}; dir="/opt/actions-cache/$(echo "$repo" | tr / _)/$sha"; \
      mkdir -p "$dir" && curl -fsSL "https://api.github.com/repos/$repo/tarball/$sha" | tar -xz -C "$dir"; \
    done \
 && chown -R runner:runner /opt/actions-cache \
 && printf 'ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=/opt/actions-cache\nACTIONS_RUNNER_SYMLINK_CACHED_ACTIONS=true\n' >> /opt/runner/.env \
 && echo "/opt/hostedtoolcache/node/${NODE_VERSION#v}/x64/bin:/opt/jdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > /opt/runner/.path \
 && chown runner:runner /opt/runner/.path

COPY ci-boot ci-trim-cache ci-memwatch ci-capacity /usr/local/sbin/
COPY ci-boot.service ci-trim-cache.service ci-memwatch.service /etc/systemd/system/
RUN chmod 0755 /usr/local/sbin/ci-boot /usr/local/sbin/ci-trim-cache /usr/local/sbin/ci-memwatch /usr/local/sbin/ci-capacity && systemctl enable ci-boot.service ci-trim-cache.service ci-memwatch.service
