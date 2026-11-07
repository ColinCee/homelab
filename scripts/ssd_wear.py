#!/usr/bin/env python3
"""Write the host SSD's lifetime bytes and wear to ssd.prom for the textfile collector.

Reads the NVMe health log (log page 0x02) with an admin ioctl, so it runs as root and
needs nothing installed. Installed by ci-runner/install-host.sh and run every 15 minutes
by homelab-ssd-wear.timer. On any error the old file stays and the unit fails.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from pathlib import Path

DEVICE = "/dev/nvme0"
OUT = Path("/var/lib/homelab-metrics/ssd.prom")
LOG_SIZE = 512
DATA_UNIT_BYTES = 512_000  # the spec counts data units of 1000 512-byte blocks

# _IOWR('N', 0x41, struct nvme_passthru_cmd), 72 bytes.
NVME_IOCTL_ADMIN_CMD = 0xC0484E41
# opcode flags rsvd nsid cdw2 cdw3 metadata addr metadata_len data_len cdw10-15 timeout result
PASSTHRU = struct.Struct("=BBHIIIQQII6III")


def read_health_log(device: str = DEVICE) -> bytes:
    buf = ctypes.create_string_buffer(LOG_SIZE)
    cdw10 = ((LOG_SIZE // 4 - 1) << 16) | 0x02  # dwords to read, minus one; log 0x02
    cmd = bytearray(
        PASSTHRU.pack(
            0x02, 0, 0, 0xFFFFFFFF, 0, 0, 0, ctypes.addressof(buf), 0, LOG_SIZE,
            cdw10, 0, 0, 0, 0, 0, 0, 0,
        )
    )  # fmt: skip
    fd = os.open(device, os.O_RDONLY)
    try:
        status = fcntl.ioctl(fd, NVME_IOCTL_ADMIN_CMD, cmd)
    finally:
        os.close(fd)
    if status != 0:
        raise OSError(f"{device}: health log read failed with NVMe status {status:#x}")
    return buf.raw


def render(log: bytes, device: str = "nvme0") -> str:
    if len(log) != LOG_SIZE:
        raise ValueError(f"health log is {len(log)} bytes, not {LOG_SIZE}")
    spare, used = log[3], log[5]
    read_units = int.from_bytes(log[32:48], "little")
    written_units = int.from_bytes(log[48:64], "little")
    label = f'{{device="{device}"}}'
    metrics = [
        ("written_bytes_total", "counter", "Bytes the host wrote to the SSD in its life.",
         written_units * DATA_UNIT_BYTES),
        ("read_bytes_total", "counter", "Bytes the host read from the SSD in its life.",
         read_units * DATA_UNIT_BYTES),
        ("percentage_used", "gauge", "Share of the SSD's rated endurance used, can pass 100.",
         used),
        ("available_spare_ratio", "gauge", "Spare blocks left, 0 to 1.", spare / 100),
    ]  # fmt: skip
    lines = []
    for name, kind, help_text, value in metrics:
        lines += [
            f"# HELP homelab_ssd_{name} {help_text}",
            f"# TYPE homelab_ssd_{name} {kind}",
            f"homelab_ssd_{name}{label} {value}",
        ]
    return "\n".join(lines) + "\n"


def write(text: str, out: Path = OUT) -> None:
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.rename(out)


def main() -> None:
    write(render(read_health_log()))


if __name__ == "__main__":
    main()
