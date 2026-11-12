"""Stop every CI VM and remove its files: to clear the host by hand. Restarting the supervisor
leaves VMs running (each is a unit of its own) and takes them back."""

from __future__ import annotations

import shutil
import sys

from vm_disks import Host, detach_disks


def stop_vms(host: Host) -> None:
    host.run(["systemctl", "stop", "homelab-ci-vm-*"], quiet=True)
    for cgroup in sorted(host.path("/sys/fs/cgroup/firecracker").glob("ci-slot*")):
        if cgroup.is_dir():
            try:
                (cgroup / "cgroup.kill").write_text("1\n")
            except OSError as e:
                print(f"stop-vms: {e}", file=sys.stderr)
    host.sleep(1)
    detach_disks(host, None)
    run_dir = host.path("/run/homelab-ci")
    for path in [*run_dir.glob("metadata-slot*.json"), *run_dir.glob("vms/*")]:
        if not path.is_dir():
            path.unlink(missing_ok=True)
    for jail in host.path("/srv/jailer/firecracker").glob("ci-slot*"):
        shutil.rmtree(jail, ignore_errors=True)


if __name__ == "__main__":
    stop_vms(Host())
