"""Stop every CI VM and remove its files: to clear the host by hand. Restarting the supervisor
leaves VMs running (each is a unit of its own) and takes them back."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable

from vm_disks import Host, detach_disks


def stop_vms(host: Host) -> int:
    """Run every step even when one fails, so one missing program or stuck snapshot leaves the
    rest cleared; the status is 1 if any failed."""
    failed = False

    def step(name: str, action: Callable[[], object]) -> None:
        nonlocal failed
        try:
            if action() is False:
                raise RuntimeError("something stayed")
        except Exception as e:
            print(f"stop-vms: {name}: {e}", file=sys.stderr)
            failed = True

    def kill_cgroups() -> None:
        for cgroup in sorted(host.path("/sys/fs/cgroup/firecracker").glob("ci-slot*")):
            if cgroup.is_dir():
                step(
                    f"kill {cgroup.name}",
                    lambda cgroup=cgroup: (cgroup / "cgroup.kill").write_text("1\n"),
                )

    def remove_registrations() -> None:
        run_dir = host.path("/run/homelab-ci")
        for path in [*run_dir.glob("metadata-slot*.json"), *run_dir.glob("vms/*")]:
            if not path.is_dir():
                path.unlink(missing_ok=True)

    def remove_jails() -> None:
        for jail in host.path("/srv/jailer/firecracker").glob("ci-slot*"):
            shutil.rmtree(jail, ignore_errors=True)

    step("stop units", lambda: host.run(["systemctl", "stop", "homelab-ci-vm-*"], quiet=True))
    step("kill VMs", kill_cgroups)
    host.sleep(1)
    step("remove snapshots", lambda: detach_disks(host, None))
    step("remove registrations", remove_registrations)
    step("remove jails", remove_jails)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(stop_vms(Host()))
