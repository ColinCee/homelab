# Root filesystem for the CI microVMs, built with Docker and exported to ext4 by
# build-rootfs.sh. Boots Ubuntu's own kernel with systemd as init. No SSH server,
# no host keys, no credentials: each VM learns its job from Firecracker's MMDS.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv udev iproute2 ca-certificates curl jq netcat-openbsd iputils-ping \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && systemctl mask systemd-resolved.service systemd-networkd.service \
      systemd-timesyncd.service apt-daily.timer apt-daily-upgrade.timer \
      e2scrub_reap.service getty@tty1.service serial-getty@ttyS0.service \
 && rm -f /etc/machine-id /var/lib/dbus/machine-id && : > /etc/machine-id \
 && useradd --create-home --shell /bin/bash runner

COPY ci-boot /usr/local/sbin/ci-boot
COPY ci-boot.service /etc/systemd/system/ci-boot.service
RUN chmod 0755 /usr/local/sbin/ci-boot && systemctl enable ci-boot.service
