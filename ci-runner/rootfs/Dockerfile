# Root filesystem for the CI microVMs, built with Docker and exported to ext4 by
# build-rootfs.sh. Boots Ubuntu's own kernel with systemd as init. No SSH server,
# no host keys, no credentials: each VM learns its job from Firecracker's MMDS.
# Workflows install their own tools (setup-uv, setup-node) and use GitHub's
# cache, which keeps branches from writing main's entries.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv udev iproute2 ca-certificates curl jq netcat-openbsd iputils-ping \
      git sudo unzip xz-utils zstd build-essential \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-resolved.service systemd-networkd.service \
      systemd-timesyncd.service apt-daily.timer apt-daily-upgrade.timer \
      e2scrub_reap.service getty@tty1.service serial-getty@ttyS0.service \
 && rm -f /etc/machine-id /var/lib/dbus/machine-id && : > /etc/machine-id \
 && useradd --create-home --shell /bin/bash runner \
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
# Root inside the VM is fine: the VM is the boundary, and it is thrown away.

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
 && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
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
 && UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install "${PYTHON_VERSION}" \
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

# Actions the CI repos use, unpacked where the runner looks before downloading
# (<owner>_<repo>/<commit>/<the archive's own folder>), and Node and the JDK on the
# jobs' PATH.
ARG CACHED_ACTIONS
RUN for a in ${CACHED_ACTIONS}; do \
      repo=${a%@*}; sha=${a#*@}; dir="/opt/actions-cache/$(echo "$repo" | tr / _)/$sha"; \
      mkdir -p "$dir" && curl -fsSL "https://api.github.com/repos/$repo/tarball/$sha" | tar -xz -C "$dir"; \
    done \
 && printf 'ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=/opt/actions-cache\nACTIONS_RUNNER_SYMLINK_CACHED_ACTIONS=true\n' >> /opt/runner/.env \
 && echo "/opt/hostedtoolcache/node/${NODE_VERSION#v}/x64/bin:/opt/jdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > /opt/runner/.path \
 && chown runner:runner /opt/runner/.path

COPY ci-boot /usr/local/sbin/ci-boot
COPY ci-boot.service /etc/systemd/system/ci-boot.service
RUN chmod 0755 /usr/local/sbin/ci-boot && systemctl enable ci-boot.service
