"""Every systemd unit of the host keeps the same settings for the same kind of work."""

import re
from pathlib import Path

from conftest import units

ROOT = Path(__file__).parent.parent
# The host's own: the CI VM image's units (rootfs/) run inside a VM.
UNITS = sorted(u for u in units() if "rootfs" not in u.parts)
# System units that change the host itself, so cannot run read-only or without privileges.
CHANGE_THE_HOST = {
    # Runs the Firecracker VMs as root, through the jailer.
    "homelab-ci-supervisor.service",
    # Drives VMs in every slot, as the supervisor does.
    "homelab-ci-load-test.service",
    # Installs into /usr/local, /etc and systemd.
    "homelab-ci-update.service",
    # Sets nftables rules, sysctls and the VMs' taps.
    "homelab-ci-net.service",
}
# Started by a timer and doing their work in their own process, so they run below the services.
NICE_EXEMPT = {
    # The work runs in dockerd or a container; nice applies only to the docker client.
    "backup-upkeep.service",
    "home-assistant-history.service",
    # A deploy is waited on: by the doorbell's ring, and by the "Main not deployed" alert.
    "deploy-main.service",
}


def settings(unit: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for key, value in re.findall(r"^(\w+)=(.*)$", unit.read_text(encoding="utf-8"), re.M):
        found.setdefault(key, []).append(value)
    return found


def test_every_oneshot_has_a_start_limit() -> None:
    # A system oneshot has no limit by default and a user one 90 s: one says how long its run takes.
    for unit in UNITS:
        if settings(unit).get("Type") == ["oneshot"]:
            assert "TimeoutStartSec" in settings(unit), f"{unit.name}: set TimeoutStartSec"


def test_system_units_run_read_only_without_new_privileges_unless_they_change_the_host() -> None:
    # A stack's units run in the user's systemd; the rest are root's.
    system = [u for u in UNITS if u.relative_to(ROOT).parts[0] != "stacks"]
    assert {u.name for u in system} >= CHANGE_THE_HOST
    for unit in system:
        hardened = {"NoNewPrivileges": ["yes"], "ProtectSystem": ["strict"]}
        has = {k: settings(unit).get(k) for k in hardened}
        if unit.name in CHANGE_THE_HOST:
            assert has != hardened, f"{unit.name} is hardened now: drop it from CHANGE_THE_HOST"
        else:
            assert has == hardened, f"{unit.name}: NoNewPrivileges=yes and ProtectSystem=strict"


def test_timer_work_runs_below_the_services() -> None:
    timed = {u.with_suffix(".timer").name for u in UNITS if u.with_suffix(".timer").is_file()}
    assert {u.name for u in UNITS} >= NICE_EXEMPT
    for unit in UNITS:
        if unit.with_suffix(".timer").name not in timed:
            continue
        nice = settings(unit).get("Nice")
        if unit.name in NICE_EXEMPT:
            assert not nice, f"{unit.name} sets Nice now: drop it from NICE_EXEMPT"
        else:
            assert nice == ["10"], f"{unit.name}: Nice=10"


def test_units_write_metrics_files_only_through_telemetry() -> None:
    # A shell's `> file.tmp && mv` is replace_file done a second way (tests/test_telemetry.py).
    for unit in UNITS:
        for key, values in settings(unit).items():
            if key.startswith("Exec"):
                for value in values:
                    assert ".prom" not in value, (
                        f"{unit.name}: write it with telemetry.replace_file"
                    )
