"""The intrusion alerts' facts (scripts/security_facts.py) turn the journal, `tailscale status`
and the runner guard into security.prom."""

from pathlib import Path

from scripts import security_facts

STATUS = {
    "Self": {"DNSName": "beelink.tail4f465e.ts.net.", "Created": "2026-01-31T18:28:12.625380059Z"},
    "Peer": {
        "a": {
            "DNSName": "desktop.tail4f465e.ts.net.",
            "Created": "2026-01-31T18:28:48.59761034Z",
            "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::10"],
        },
        # Its host name is "localhost", so a device is named by its DNS name.
        "b": {
            "DNSName": "phone.tail4f465e.ts.net.",
            "HostName": "localhost",
            "Created": "2026-02-03T22:15:42Z",
        },
    },
}


def entry(identifier: str, message: object, seconds: int) -> dict:
    return {
        "SYSLOG_IDENTIFIER": identifier,
        "MESSAGE": message,
        "__REALTIME_TIMESTAMP": str(seconds * 10**6),
    }


def test_devices_are_named_by_dns_name_with_when_they_were_added() -> None:
    added, names = security_facts.tailnet(STATUS)
    assert added == {
        "beelink": 1769884092.62538,
        "desktop": 1769884128.59761,
        "phone": 1770156942.0,
    }
    assert names["100.64.0.10"] == "desktop"


def test_each_event_keeps_its_newest_time_and_who() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    entries = [
        entry("homelab-deploy", f"commit={sha} actor=ColinCee", 100),
        entry("homelab-deploy", f"commit={sha} actor=ColinCee", 160),
        entry("homelab-ci-update", f"installed commit={sha} author=Colin Cheung", 120),
        # update.sh's other lines are not installs.
        entry("homelab-ci-update", f"on {sha}, image built at {sha}", 130),
        entry(
            "homelab-runner-guard",
            "mode=enforce verdict=allow GITHUB_REPOSITORY=ColinCee/homelab",
            140,
        ),
        entry(
            "homelab-runner-guard",
            "mode=enforce verdict=refuse reason='workflow x is not allowed' "
            "GITHUB_REPOSITORY=someone/fork GITHUB_EVENT_NAME=pull_request GITHUB_ACTOR=someone",
            150,
        ),
        entry(
            "sshd",
            "Accepted publickey for ci-peer from 100.64.0.10 port 58736 ssh2: ED25519 SHA256:x",
            170,
        ),
        entry("sshd", "Connection closed by 100.100.146.119 port 54316", 175),
        entry(
            "tailscaled",
            "ssh-session(sess-1): starting non-pty command: [/usr/sbin/tailscaled be-child ssh "
            "--uid=1000 --local-user=colin --home-dir=/home/colin --remote-user=a@b.c "
            "--remote-ip=203.0.113.9 --has-tty=false]",
            180,
        ),
        # A line that is not UTF-8 comes as a list of bytes.
        entry("sshd", [65, 255], 190),
    ]
    _, names = security_facts.tailnet(STATUS)

    found = security_facts.events(entries, names)

    assert found == {
        "deploy": {("ColinCee", "0123456789ab"): 160},
        "ci_host_install": {("Colin Cheung", "0123456789ab"): 120},
        "runner_guard_refusal": {("someone/fork", "someone"): 150},
        "ssh_login": {
            ("ci-peer", "100.64.0.10", "desktop"): 170,
            ("colin", "203.0.113.9", ""): 180,
        },
    }


def test_the_file_holds_each_fact_with_its_labels(tmp_path: Path) -> None:
    found = {kind: {} for kind in security_facts.LABELS}
    found["ci_host_install"] = {('Colin "C"', "0123456789ab"): 120.0}
    out = tmp_path / "security.prom"

    found["ssh_login"] = {
        ("colin", "100.64.0.10", "desktop"): 130.0,
        ("root", "203.0.113.9", ""): 140.0,
    }
    rendered = security_facts.render(found, {"laptop": 1794080335.1}, False, {"desktop"})
    security_facts.write(rendered, out)

    samples = [line for line in out.read_text().splitlines() if not line.startswith("#")]
    assert samples == [
        "homelab_ci_host_install_timestamp_seconds"
        '{author="Colin \\"C\\"",commit="0123456789ab"} 120',
        'homelab_ssh_login_timestamp_seconds{user="colin",from="100.64.0.10",device="desktop",'
        'known="true"} 130',
        'homelab_ssh_login_timestamp_seconds{user="root",from="203.0.113.9",device="",'
        'known="false"} 140',
        'homelab_tailscale_device_created_timestamp_seconds{name="laptop",known="false"} '
        "1794080335",
        "homelab_runner_guard_matches_main 0",
    ]
    assert [p.name for p in tmp_path.iterdir()] == ["security.prom"]


def test_the_guard_matches_main_only_byte_for_byte(tmp_path: Path) -> None:
    installed, main = tmp_path / "installed.py", tmp_path / "main.py"
    installed.write_text("ALLOW = 1\n")
    main.write_text("ALLOW = 1\n")
    assert security_facts.same_file(installed, main)
    main.write_text("ALLOW = 2\n")
    assert not security_facts.same_file(installed, main)
    assert not security_facts.same_file(tmp_path / "gone.py", main)


def test_the_first_run_learns_the_tailnet_and_a_later_device_stays_unknown(tmp_path: Path) -> None:
    state = tmp_path / "known-devices.json"

    assert security_facts.known_devices(["desktop", "phone"], state) == {"desktop", "phone"}
    assert security_facts.known_devices(["desktop", "phone", "stranger"], state) == {
        "desktop",
        "phone",
    }
    assert state.stat().st_mode & 0o777 == 0o600
