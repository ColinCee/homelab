# Root filesystem for the CI microVMs, built with Docker and exported to ext4 by
# build-rootfs.sh. Boots Ubuntu's own kernel with systemd as init. No SSH server,
# no host keys, no credentials: each VM learns its job from Firecracker's MMDS.
# Workflows install their own tools (setup-uv, setup-node) and use GitHub's
# cache, which keeps branches from writing main's entries.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

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
 && chown -R runner:runner /opt/hostedtoolcache \
 && printf 'RUNNER_TOOL_CACHE=/opt/hostedtoolcache\nUV_PYTHON_INSTALL_DIR=/opt/uv-python\n' >> /opt/runner/.env

# Actions the CI repos use, unpacked where the runner looks before downloading
# (<owner>_<repo>/<commit>/<the archive's own folder>), and Node on the jobs' PATH.
ARG CACHED_ACTIONS
RUN for a in ${CACHED_ACTIONS}; do \
      repo=${a%@*}; sha=${a#*@}; dir="/opt/actions-cache/$(echo "$repo" | tr / _)/$sha"; \
      mkdir -p "$dir" && curl -fsSL "https://api.github.com/repos/$repo/tarball/$sha" | tar -xz -C "$dir"; \
    done \
 && printf 'ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE=/opt/actions-cache\nACTIONS_RUNNER_SYMLINK_CACHED_ACTIONS=true\n' >> /opt/runner/.env \
 && echo "/opt/hostedtoolcache/node/${NODE_VERSION#v}/x64/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > /opt/runner/.path \
 && chown runner:runner /opt/runner/.path

COPY ci-boot /usr/local/sbin/ci-boot
COPY ci-boot.service /etc/systemd/system/ci-boot.service
RUN chmod 0755 /usr/local/sbin/ci-boot && systemctl enable ci-boot.service
