"""Every alert rule (stacks/observability/prometheus/rules.yml) meets the alert standard in
docs/observability.md, and the rules fire, hold and clear as the standard says: promtool runs
them over made-up series, so those tests need Docker."""

import functools
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

STACK = Path(__file__).parent.parent / "stacks/observability"
GROUPS = yaml.safe_load((STACK / "prometheus/rules.yml").read_text(encoding="utf-8"))["groups"]
RULES = {r["alert"]: r for g in GROUPS for r in g["rules"]}
HOMELAB = {r["alert"]: r for r in GROUPS[0]["rules"]}
COMPOSE = yaml.safe_load((STACK / "compose.yaml").read_text(encoding="utf-8"))
PROMETHEUS = COMPOSE["services"]["prometheus"]["image"]
HOME = "http://beelink.tail4f465e.ts.net:5080/d/home"


def flat(text: str) -> str:
    return " ".join(text.split())


def seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", duration)
    assert match, duration
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


@pytest.mark.parametrize("name", RULES)
def test_a_rule_says_its_level_what_broke_why_it_matters_and_what_to_do(name: str) -> None:
    rule = RULES[name]
    notes = rule["annotations"]
    assert rule["labels"] == {"level": rule["labels"]["level"]}
    assert rule["labels"]["level"] in ("now", "week", "watchdog")
    assert len(notes["title"].split()) <= 8, "say the symptom in a few words"
    assert notes["measure"] and "unit" in notes
    # A sentence each, not a placeholder.
    assert len(notes["harm"].split()) >= 6 and len(notes["action"].split()) >= 6
    assert notes["dashboard"] == HOME and notes["value"] == "{{ $value }}"
    seconds(rule["for"])
    # The hold: a return within 10 minutes is the same alert.
    assert rule["keep_firing_for"] == "10m"


def test_no_data_is_kept_for_metrics_alloy_does_not_make() -> None:
    # A gone Alloy metric is "Beelink metrics stopped"; a textfile metric's writer can stop alone.
    for name, rule in HOMELAB.items():
        expr = flat(rule["expr"])
        no_data = re.search(r"or on\(\) max\((absent_over_time\(.+\))\) \* -1$", expr)
        if no_data:
            assert re.search(r"homelab_|node_textfile", no_data[1]), name
            window = re.search(r"\[(\d+[mh])\]\)$", no_data[1])
            # Longer than the inhibit's own window, so it never fires before it.
            assert window and seconds(window[1]) > 5 * 60, name
        elif name != "beelink-metrics-stopped":
            assert not re.search(r"homelab_(backup|pc_backup|probe)", expr), name


def test_a_rule_names_only_the_labels_that_tell_its_series_apart() -> None:
    # The post's title prints every label but alertname and level, so a rule aggregates.
    for name, rule in RULES.items():
        expr = flat(rule["expr"])
        aggregated = r"\(* ?(max|min|sum|count|vector|round\(\(time\(\) - max|round\(max)"
        assert re.match(aggregated, expr), name


@functools.cache
def docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


needs_docker = pytest.mark.skipif("not docker()", reason="needs Docker")


def promtool(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm", "--entrypoint", "promtool"]
    command += ["-v", f"{STACK / 'prometheus'}:/etc/prometheus:ro", "-v", f"{tmp_path}:/t:ro"]
    return subprocess.run(
        [*command, PROMETHEUS, *args], capture_output=True, text=True, timeout=300
    )


@needs_docker
def test_the_config_and_rules_load(tmp_path: Path) -> None:
    checked = promtool(tmp_path, "check", "config", "/etc/prometheus/prometheus.yml")
    assert checked.returncode == 0, checked.stdout + checked.stderr


def posts(alert: str, value: str, **labels: str) -> dict:
    """What a firing rule hands Alertmanager: its labels and its filled-in annotations."""
    rule = RULES[alert]
    notes = {k: v.replace("{{ $value }}", value) for k, v in rule["annotations"].items()}
    return {"exp_labels": rule["labels"] | labels, "exp_annotations": notes}


def at(minutes: int, name: str, *firing: dict) -> dict:
    return {"eval_time": f"{minutes}m", "alertname": name, "exp_alerts": list(firing)}


# Series one sample a minute, in promtool's notation ("5x20": 21 samples of 5).
CASES = {
    "fires after `for`, holds through a short dip, clears after 10 clear minutes": (
        [
            ('node_memory_MemTotal_bytes{job="integrations/unix",instance="beelink"}', "100x70"),
            # 95% used to 20 min, a 2-minute dip, 95% again to 32 min, then fine.
            (
                'node_memory_MemAvailable_bytes{job="integrations/unix",instance="beelink"}',
                "5x20 50x2 5x9 50x40",
            ),
        ],
        [
            at(4, "high-ram"),
            at(5, "high-ram", posts("high-ram", "95")),
            at(22, "high-ram", posts("high-ram", "95")),
            at(42, "high-ram", posts("high-ram", "95")),
            at(45, "high-ram"),
        ],
    ),
    "a textfile metric gone is no data, after `for`": (
        [('up{job="docker"}', "1x30")],
        [
            at(4, "home-assistant-down"),
            at(6, "home-assistant-down", posts("home-assistant-down", "-1")),
        ],
    ),
    "a failed probe fires and a passing one does not": (
        [
            ('homelab_probe_success{target="home-assistant"}', "1x10 0x20"),
            ('homelab_probe_success{target="names-dns"}', "1x30"),
            ('homelab_probe_success{target="names-proxy"}', "1x30"),
            ("homelab_probe_timestamp_seconds", "0+60x30"),
        ],
        [
            at(8, "home-assistant-down"),
            at(8, "dns-or-names-down"),
            at(16, "home-assistant-down", posts("home-assistant-down", "0")),
        ],
    ),
    "each container is its own alert": (
        [
            ('container_start_time_seconds{job="docker",name="web",id="/a"}', "1+1x40"),
            ('container_start_time_seconds{job="docker",name="db",id="/b"}', "1x40"),
        ],
        [
            # Over 2 starts from 3 min on, then 10 min of `for`.
            at(12, "restart-loop"),
            at(13, "restart-loop", posts("restart-loop", "13", name="web")),
        ],
    ),
    "a container outside Compose fires after 15 minutes, one in a project never": (
        [
            ('container_last_seen{job="docker",name="byhand"}', "1x30"),
            (
                'container_last_seen{job="docker",name="web",'
                'container_label_com_docker_compose_project="app"}',
                "1x30",
            ),
        ],
        [
            at(14, "new-container"),
            at(16, "new-container", posts("new-container", "1", name="byhand")),
        ],
    ),
    "a store that was never sent data is not named": (
        # Nothing sends spans while OBI is off.
        [('vt_rows_ingested_total{job="traces"}', "0x50")],
        [at(40, "store-stopped")],
    ),
    "a store taking nothing is named, one still taking data is not": (
        [
            ('vl_rows_ingested_total{job="logs",type="opentelemetry"}', "0+60x10 600x30"),
            ('vt_rows_ingested_total{job="traces"}', "0+60x50"),
            ('prometheus_tsdb_head_samples_appended_total{job="prometheus"}', "0+600x50"),
        ],
        [
            # Flat from 10 min, so the 15-minute rate is 0 from 24, then 15 min of `for`.
            at(38, "store-stopped"),
            at(40, "store-stopped", posts("store-stopped", "0", job="logs")),
        ],
    ),
    # Colin's and Renovate's never post.
    "another's deploy and CI-host install post who and what, then clear": (
        [
            (
                'homelab_deploy_timestamp_seconds{actor="mallory",commit="0123456789ab"}',
                "_x5 300x55",
            ),
            ('homelab_deploy_timestamp_seconds{actor="ColinCee",commit="1"}', "_x5 300x55"),
            ('homelab_deploy_timestamp_seconds{actor="renovate[bot]",commit="2"}', "_x5 300x55"),
            (
                'homelab_ci_host_install_timestamp_seconds{author="mallory",commit="ba9876543210"}',
                "_x5 300x55",
            ),
            (
                'homelab_ci_host_install_timestamp_seconds{author="Colin Cheung",commit="3"}',
                "_x5 300x55",
            ),
            (
                'homelab_ci_host_install_timestamp_seconds{author="renovate[bot]",commit="4"}',
                "_x5 300x55",
            ),
        ],
        [
            at(4, "deploy-ran"),
            at(6, "deploy-ran", posts("deploy-ran", "1", actor="mallory", commit="0123456789ab")),
            at(
                6,
                "ci-host-updated",
                posts("ci-host-updated", "1", author="mallory", commit="ba9876543210"),
            ),
            at(13, "deploy-ran", posts("deploy-ran", "8", actor="mallory", commit="0123456789ab")),
            # Not under 10 minutes from 15, then the 10-minute hold.
            at(30, "deploy-ran"),
        ],
    ),
    "the watchdog fires while Discord takes posts and stops for an hour after one fails": (
        [('alertmanager_notifications_failed_total{integration="discord"}', "0x10 1x80")],
        [
            at(5, "watchdog", posts("watchdog", "1")),
            # The failure at 11 min is in the last hour until 71, then the 10-minute hold.
            at(25, "watchdog"),
            at(82, "watchdog", posts("watchdog", "1")),
        ],
    ),
    "a re-added Tailscale device fires for its first hour, a listed one never": (
        [
            # Added a day before the series start.
            (
                'homelab_tailscale_device_created_timestamp_seconds{name="beelink",known="true"}',
                "-86400x100",
            ),
            (
                'homelab_tailscale_device_created_timestamp_seconds{name="laptop",known="true"}',
                "_x10 600x90",
            ),
        ],
        [
            at(9, "tailscale-device-added"),
            at(11, "tailscale-device-added", posts("tailscale-device-added", "0", name="laptop")),
            # An hour after it was added, then the hold.
            at(85, "tailscale-device-added"),
        ],
    ),
    "a device not on the list fires while it is there": (
        [('homelab_tailscale_device_created_timestamp_seconds{name="stranger"}', "0x100")],
        [at(95, "tailscale-device-added", posts("tailscale-device-added", "2", name="stranger"))],
    ),
    "a runner guard that is not main's fires after 15 minutes": (
        [("homelab_runner_guard_matches_main", "1x10 0x30")],
        [
            at(25, "runner-guard-changed"),
            at(26, "runner-guard-changed", posts("runner-guard-changed", "0")),
        ],
    ),
    "a refused job and an SSH login from elsewhere fire, the PC's login does not": (
        [
            (
                'homelab_runner_guard_refusal_timestamp_seconds{repository="someone/fork",actor="someone"}',
                "_x5 300x30",
            ),
            (
                'homelab_ssh_login_timestamp_seconds{user="colin",from="100.64.0.10",device="desktop",known="true"}',
                "_x5 300x30",
            ),
            (
                'homelab_ssh_login_timestamp_seconds{user="root",from="203.0.113.9",known="false"}',
                "_x5 300x30",
            ),
        ],
        [
            at(
                6,
                "runner-guard-refused",
                posts("runner-guard-refused", "1", repository="someone/fork", actor="someone"),
            ),
            at(
                6,
                "ssh-login-elsewhere",
                posts("ssh-login-elsewhere", "1", user="root", **{"from": "203.0.113.9"}),
            ),
        ],
    ),
    "security checks stopping fires after 10 minutes and `for`": (
        [
            (
                'node_textfile_mtime_seconds{file="/var/lib/homelab-metrics/security.prom"}',
                "0+60x10 600x30",
            )
        ],
        [
            at(25, "security-checks-stopped"),
            at(27, "security-checks-stopped", posts("security-checks-stopped", "17")),
        ],
    ),
    "a failed upkeep fires after `for`, a good one after it clears": (
        [
            ("homelab_upkeep_last_success_timestamp_seconds", "0x80 5000x60"),
            ("homelab_upkeep_last_failure_timestamp_seconds", "_x5 60x135"),
        ],
        [
            at(4, "upkeep-failed"),
            at(66, "upkeep-failed", posts("upkeep-failed", "0")),
            at(95, "upkeep-failed"),
        ],
    ),
    "metrics stopping is one alert with no labels": (
        [('up{job="integrations/unix"}', "1x5"), ('up{job="docker"}', "1x5")],
        [
            at(5, "beelink-metrics-stopped"),
            at(12, "beelink-metrics-stopped", posts("beelink-metrics-stopped", "1")),
        ],
    ),
}


@needs_docker
@pytest.mark.parametrize("case", CASES)
def test_rules_fire_hold_and_clear_as_the_standard_says(tmp_path: Path, case: str) -> None:
    series, checks = CASES[case]
    tests = {
        "rule_files": ["/etc/prometheus/rules.yml"],
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": [{"series": s, "values": v} for s, v in series],
                "alert_rule_test": checks,
            }
        ],
    }
    (tmp_path / "test.yml").write_text(json.dumps(tests), encoding="utf-8")
    # promtool runs as nobody, and pytest makes the folder for its owner only.
    tmp_path.chmod(0o755)
    (tmp_path / "test.yml").chmod(0o644)
    ran = promtool(tmp_path, "test", "rules", "/t/test.yml")
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_replay_posts_as_prometheus_and_alertmanager_would() -> None:
    from stacks.observability.prometheus import replay

    # The query returns for 5 h, not for 4 minutes, for 5 h more, then stops.
    returned = {60 * m for m in [*range(0, 300), *range(304, 604)]}
    found = replay.posts(returned, RULES["high-ram"], 0, 60 * 620)
    assert found == [
        (60 * 5, "firing"),
        (60 * 245, "repeat"),
        (60 * 485, "repeat"),
        (60 * 613, "cleared"),
    ]
