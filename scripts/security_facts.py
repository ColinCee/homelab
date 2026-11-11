#!/usr/bin/env python3
"""Write what the intrusion alerts read (prometheus/rules.yml, group intrusion) to security.prom:
the last hour's deploys, CI-host installs, runner guard refusals and SSH logins from the journal,
every tailnet device and when it was added, and whether the installed runner guard is main's.
A device, or an SSH login's device, is known="true" only if it was on the tailnet at the first run
(STATE), so the repository names none.

A host job, not a container, so no container needs a path to the host or its Tailscale. (Alloy
reads the journal too, for the log store, without sudo's or tailscaled's lines.) Installed by
ci-runner/install-host.sh, run each minute by
homelab-security-facts.timer. On any error the old file stays and the unit fails, so "Security
checks stopped" fires.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

OUT = Path("/var/lib/homelab-metrics/security.prom")
# Installed by hand (scripts/install-runner-guard.sh); main's copy is the CI host's checkout, which
# update.sh moves within a minute of each merge.
GUARD = Path("/usr/local/lib/homelab/runner_job_guard.py")
MAIN_GUARD = Path("/var/lib/homelab-ci/src/scripts/runner_job_guard.py")
# The unit's StateDirectory. Delete it to learn the tailnet as it is now.
STATE = Path("/var/lib/homelab-security-facts/known-devices.json")
# deploy.yaml's first step, update.sh after an install, the runner guard, sshd, and Tailscale SSH.
IDENTIFIERS = (
    "homelab-deploy",
    "homelab-ci-update",
    "homelab-runner-guard",
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
    "deploy": ("actor", "commit"),
    "ci_host_install": ("author", "commit"),
    "runner_guard_refusal": ("repository", "actor"),
    "ssh_login": ("user", "from", "device"),
}
HELP = {
    "deploy": "When deploy.yaml last started for this actor and commit, in the last hour.",
    "ci_host_install": "When update.sh last installed this commit as root, in the last hour.",
    "runner_guard_refusal": "When the runner guard last refused a job, in the last hour.",
    "ssh_login": "When this user last logged in over SSH from this address, in the last hour.",
}

Events = dict[str, dict[tuple[str, ...], float]]


def journal(since: str = "-1h") -> list[dict]:
    command = ["journalctl", "--output=json", "--no-pager", "--quiet", f"--since={since}"]
    command += [f"--identifier={name}" for name in IDENTIFIERS]
    lines = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60).stdout
    return [json.loads(line) for line in lines.splitlines() if line]


def events(entries: Iterable[dict], devices: dict[str, str]) -> Events:
    """The newest time of each event, by kind and labels; `devices` names tailnet addresses."""
    found: Events = {kind: {} for kind in LABELS}
    for entry in entries:
        message = entry.get("MESSAGE")
        # The journal gives a line that is not UTF-8 as a list of bytes.
        if not isinstance(message, str):
            continue
        name = entry.get("SYSLOG_IDENTIFIER")
        fields = dict(FIELDS.findall(message))
        if name == "homelab-deploy":
            kind, values = "deploy", (fields.get("actor", ""), fields.get("commit", "")[:12])
        elif name == "homelab-ci-update" and message.startswith("installed "):
            # The author's name may hold spaces, so it is the rest of the line.
            kind, values = (
                "ci_host_install",
                (message.partition(" author=")[2], fields.get("commit", "")[:12]),
            )
        elif name == "homelab-runner-guard" and fields.get("verdict") == "refuse":
            kind, values = (
                "runner_guard_refusal",
                (fields.get("GITHUB_REPOSITORY", ""), fields.get("GITHUB_ACTOR", "")),
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


def tailnet(status: dict) -> tuple[dict[str, float], dict[str, str]]:
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
        _replace(state, json.dumps(sorted(current)) + chr(10), 0o600)
    return set(json.loads(state.read_text(encoding="utf-8")))


def tailscale_status() -> dict:
    run = subprocess.run(
        ["tailscale", "status", "--json"], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(run.stdout)


def same_file(a: Path, b: Path) -> bool:
    """False when either is missing: a guard gone is not main's either."""
    try:
        return hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()
    except FileNotFoundError:
        return False


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(
    found: Events, added: dict[str, float], guard_matches: bool, known: set[str] = frozenset()
) -> str:
    lines = []
    for kind, names in LABELS.items():
        metric = f"homelab_{kind}_timestamp_seconds"
        lines += [f"# HELP {metric} {HELP[kind]}", f"# TYPE {metric} gauge"]
        for values, when in sorted(found[kind].items()):
            pairs = ",".join(f'{n}="{label(v)}"' for n, v in zip(names, values, strict=True))
            if kind == "ssh_login":
                pairs += f',known="{str(values[2] in known).lower()}"'
            lines.append(f"{metric}{{{pairs}}} {when:.0f}")
    metric = "homelab_tailscale_device_created_timestamp_seconds"
    lines += [f"# HELP {metric} When each tailnet device was added.", f"# TYPE {metric} gauge"]
    lines += [
        f'{metric}{{name="{label(name)}",known="{str(name in known).lower()}"}} {when:.0f}'
        for name, when in sorted(added.items())
    ]
    lines += [
        "# HELP homelab_runner_guard_matches_main Whether the guard is main's (1 yes, 0 no).",
        "# TYPE homelab_runner_guard_matches_main gauge",
        f"homelab_runner_guard_matches_main {int(guard_matches)}",
    ]
    return "\n".join(lines) + "\n"


def write(text: str, out: Path = OUT) -> None:
    _replace(out, text, 0o644)


def _replace(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    tmp.rename(path)


def main() -> None:
    added, names = tailnet(tailscale_status())
    known = known_devices(added)
    write(render(events(journal(), names), added, same_file(GUARD, MAIN_GUARD), known))


if __name__ == "__main__":
    main()
