#!/usr/bin/env python3
"""Write what the intrusion alerts read (prometheus/rules.yml, group intrusion) to security.prom:
the last hour's CI-host installs and SSH logins from the journal, and every tailnet device and when
it was added.
Also two host facts for group homelab: whether a reboot waits, and how many security updates
wait; unattended-upgrades should clear both within a day.
A device, or an SSH login's device, is known="true" only if it was on the tailnet at the first run
(STATE), so the repository names none.

A host job, not a container, so no container needs a path to the host or its Tailscale. (Alloy
reads the journal too, for the log store, without sudo's or tailscaled's lines.) Installed by
ci-runner/install-host.sh, run each minute by
homelab-security-facts.timer. On any error the old file stays and the unit fails, so "Security
checks stopped" fires.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Set
from datetime import datetime
from pathlib import Path
from typing import Any

import telemetry

OUT = Path("/var/lib/homelab-metrics/security.prom")
# The unit's StateDirectory. Delete it to learn the tailnet as it is now.
STATE = Path("/var/lib/homelab-security-facts/known-devices.json")
# Written by packages that need a reboot; gone after one, as /run is in memory.
REBOOT = Path("/run/reboot-required")
# apt-check's summary, rewritten after each `apt update` and dpkg run (update-notifier's hook).
UPDATES = Path("/var/lib/update-notifier/updates-available")
# ESM's updates are counted on lines of their own, and those not enabled cannot be installed.
SECURITY = re.compile(r"(\d+) of these updates (?:is a|are) standard security update")
# update.sh after an install, sshd, and Tailscale SSH.
IDENTIFIERS = (
    "homelab-ci-update",
    "sshd",
    "sshd-session",
    "tailscaled",
)

FIELDS = re.compile(r"(\w+)=(\S+)")
SSH_LOGIN = re.compile(r"^Accepted \S+ for (\S+) from (\S+) port ")
TAILSCALE_SSH = re.compile(
    r"^ssh-session\(\S+\): starting .* --local-user=(\S+) .* --remote-ip=(\S+) "
)
LABELS = {
    "ci_host_install": ("author", "commit"),
    "ssh_login": ("user", "from", "device"),
}
HELP = {
    "ci_host_install": "When update.sh last installed this commit as root, in the last hour.",
    "ssh_login": "When this user last logged in over SSH from this address, in the last hour.",
}

Events = dict[str, dict[tuple[str, ...], float]]


def journal(since: str = "-1h") -> list[dict[str, Any]]:
    command = ["journalctl", "--output=json", "--no-pager", "--quiet", f"--since={since}"]
    command += [f"--identifier={name}" for name in IDENTIFIERS]
    lines = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60).stdout
    return [json.loads(line) for line in lines.splitlines() if line]


def events(entries: Iterable[dict[str, Any]], devices: dict[str, str]) -> Events:
    """The newest time of each event, by kind and labels; `devices` names tailnet addresses."""
    found: Events = {kind: {} for kind in LABELS}
    for entry in entries:
        message = entry.get("MESSAGE")
        # The journal gives a line that is not UTF-8 as a list of bytes.
        if not isinstance(message, str):
            continue
        name = entry.get("SYSLOG_IDENTIFIER")
        fields = dict(FIELDS.findall(message))
        if name == "homelab-ci-update" and message.startswith("installed "):
            # The author's name may hold spaces, so it is the rest of the line.
            kind, values = (
                "ci_host_install",
                (message.partition(" author=")[2], fields.get("commit", "")[:12]),
            )
        elif (name in ("sshd", "sshd-session") and (login := SSH_LOGIN.match(message))) or (
            name == "tailscaled" and (login := TAILSCALE_SSH.match(message))
        ):
            user, address = login.groups()
            kind, values = "ssh_login", (user, address, devices.get(address, ""))
        else:
            continue
        when = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        found[kind][values] = max(when, found[kind].get(values, 0.0))
    return found


def tailnet(status: dict[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    """When each device was added, by its DNS name's first label (its host name can be
    "localhost"), and the device of each address."""
    added: dict[str, float] = {}
    names: dict[str, str] = {}
    for device in [status["Self"], *(status.get("Peer") or {}).values()]:
        name = device["DNSName"].split(".")[0] or device.get("HostName", "")
        # Go writes nanoseconds, which fromisoformat takes only to the microsecond.
        stamp = re.sub(r"(\.\d{6})\d*", r"\1", device["Created"]).replace("Z", "+00:00")
        added[name] = datetime.fromisoformat(stamp).timestamp()
        names.update(dict.fromkeys(device.get("TailscaleIPs") or [], name))
    return added, names


def known_devices(current: Iterable[str], state: Path = STATE) -> set[str]:
    """The devices on the tailnet when the state was first written; a later one stays unknown."""
    if not state.exists():
        telemetry.replace_file(state, json.dumps(sorted(current)) + chr(10), 0o600)
    return set(json.loads(state.read_text(encoding="utf-8")))


def tailscale_status() -> dict[str, Any]:
    run = subprocess.run(
        ["tailscale", "status", "--json"], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(run.stdout)


def render(found: Events, added: dict[str, float], known: Set[str] = frozenset()) -> str:
    lines: list[str] = []
    for kind, names in LABELS.items():
        metric = f"homelab_{kind}_timestamp_seconds"
        lines += [f"# HELP {metric} {HELP[kind]}", f"# TYPE {metric} gauge"]
        for values, when in sorted(found[kind].items()):
            pairs = ",".join(
                f'{n}="{telemetry.label(v)}"' for n, v in zip(names, values, strict=True)
            )
            if kind == "ssh_login":
                pairs += f',known="{str(values[2] in known).lower()}"'
            lines.append(f"{metric}{{{pairs}}} {when:.0f}")
    metric = "homelab_tailscale_device_created_timestamp_seconds"
    lines += [f"# HELP {metric} When each tailnet device was added.", f"# TYPE {metric} gauge"]
    lines += [
        f'{metric}{{name="{telemetry.label(name)}",known="{str(name in known).lower()}"}}'
        f" {when:.0f}"
        for name, when in sorted(added.items())
    ]
    return "\n".join(lines) + "\n"


def host_facts(reboot: Path = REBOOT, updates: Path = UPDATES) -> str:
    """No security metric when apt's summary is missing, unreadable or worded otherwise, so its
    rule fires as no data rather than reading 0; the intrusion facts are written either way."""
    lines = [
        "# HELP homelab_reboot_required Whether an update waits on a reboot (1 yes, 0 no).",
        "# TYPE homelab_reboot_required gauge",
        f"homelab_reboot_required {int(reboot.exists())}",
    ]
    try:
        summary = updates.read_text(encoding="utf-8", errors="replace")
    except OSError:
        summary = ""
    if "can be applied immediately" in summary:
        found = SECURITY.search(summary)
        lines += [
            "# HELP homelab_security_updates_pending Security updates apt can install now.",
            "# TYPE homelab_security_updates_pending gauge",
            f"homelab_security_updates_pending {int(found[1]) if found else 0}",
        ]
    return "\n".join(lines) + "\n"


def write(text: str, out: Path = OUT) -> None:
    telemetry.replace_file(out, text)


def main() -> None:
    added, names = tailnet(tailscale_status())
    known = known_devices(added)
    write(render(events(journal(), names), added, known) + host_facts())


if __name__ == "__main__":
    telemetry.start()
    main()
