"""Every alert rule (stacks/observability/prometheus/rules.yml) meets the alert standard in
docs/architecture.md, Alertmanager carries out its lifecycle, and the rules fire, hold and clear
as the standard says: promtool runs them over made-up series."""

import itertools
import json
import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import stacks, tool

from stacks.observability.prometheus import slos

STACK = Path(__file__).parent.parent / "stacks/observability"
GROUPS = slos.rule_groups()
RULES = {r["alert"]: r for g in GROUPS for r in g["rules"]}
HOMELAB = {r["alert"]: r for r in GROUPS[0]["rules"]}
ALERTMANAGER = yaml.safe_load((STACK / "alertmanager/alertmanager.yml").read_text(encoding="utf-8"))
COMPOSE = stacks()[STACK]
HOME = "http://grafana.internal/d/home"


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


def test_beelink_metrics_stopped_holds_back_every_alert_that_reads_alloys_metrics() -> None:
    # The rules on Prometheus itself and the public probes read its own scrape, not
    # Alloy's; the store rules read Alloy's.
    assert {n for n in RULES if n.startswith("Prometheus")} == {
        "PrometheusRuleFailures",
        "PrometheusBadConfig",
    }
    assert ALERTMANAGER["inhibit_rules"] == [
        {
            "source_matchers": ['alertname="beelink-metrics-stopped"'],
            "target_matchers": [
                'alertname!~"beelink-metrics-stopped|Prometheus.+|flight-tracker-down|site-down|watchdog"'
            ],
        }
    ]


def test_a_metrics_files_age_is_watched_only_by_its_promise() -> None:
    # slos.yml gives each file one stale and no-data alert; a hand rule reading it would post twice.
    text = (STACK / "prometheus/rules.yml").read_text(encoding="utf-8")
    assert "node_textfile_mtime_seconds" not in text


def test_a_rule_names_only_the_labels_that_tell_its_series_apart() -> None:
    # The post's title prints every label but alertname and level, so a rule aggregates.
    for name, rule in RULES.items():
        expr = flat(rule["expr"])
        aggregated = r"\(* ?(max|min|sum|count|vector|round\(\(time\(\) - max|round\(max)"
        assert re.match(aggregated, expr), name


def promtool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([tool("promtool"), *args], capture_output=True, text=True, timeout=300)


def test_the_config_and_rules_load() -> None:
    # The config names the rules by their path in the container, so they are checked on their own.
    checked = promtool("check", "config", "--syntax-only", str(STACK / "prometheus/prometheus.yml"))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    checked = promtool("check", "rules", *map(str, slos.RULE_FILES))
    assert checked.returncode == 0, checked.stdout + checked.stderr


def posts(alert: str, value: str, **labels: str) -> dict[str, Any]:
    """What a firing rule hands Alertmanager: its labels and its filled-in annotations."""
    rule = RULES[alert]
    notes = {k: v.replace("{{ $value }}", value) for k, v in rule["annotations"].items()}
    return {"exp_labels": rule["labels"] | labels, "exp_annotations": notes}


def at(minutes: int, name: str, *firing: dict[str, Any]) -> dict[str, Any]:
    return {"eval_time": f"{minutes}m", "alertname": name, "exp_alerts": list(firing)}


CHECK_LABELS = {"workflow": "CI", "ci_job": "check", "runner": "vm"}
CHECK = 'homelab_ci_job_seconds{kind="main",workflow="CI",ci_job="check",runner="vm"}'
# Alloy's own series, as config.alloy's scrape of itself labels them.
RECEIVER_ID = "otelcol.receiver.otlp.programs"
RECEIVER = f'job="alloy",component_id="{RECEIVER_ID}"'
EXPORTER_ID = "otelcol.exporter.otlphttp.logs"
EXPORTER = f'job="alloy",component_id="{EXPORTER_ID}"'
TRACES = 'job="alloy",component_id="otelcol.exporter.otlphttp.traces"'

# Series one sample a minute, in promtool's notation ("5x20": 21 samples of 5); a case may add a
# third element, its step.
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
        # With OBI off, nothing sends spans.
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
    "a store taking in a flood is named, one at its usual rate is not": (
        [
            # A span a second, then 100,000 a minute.
            ('vt_rows_ingested_total{job="traces"}', "0+60x10 600+100000x30"),
            ('vl_rows_ingested_total{job="logs",type="opentelemetry"}', "0+600x40"),
        ],
        [
            # Past 1,000 a second over 15 minutes from about 19 min, then 5 min of `for`.
            at(18, "store-flooded"),
            at(30, "store-flooded", posts("store-flooded", "1667", job="traces")),
        ],
    ),
    # Colin's and Renovate's never post.
    "another's CI-host install posts who and what, then clears": (
        [
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
            at(4, "ci-host-updated"),
            at(
                6,
                "ci-host-updated",
                posts("ci-host-updated", "1", author="mallory", commit="ba9876543210"),
            ),
            # Not under 10 minutes from 15, then the 10-minute hold.
            at(30, "ci-host-updated"),
        ],
    ),
    "the watchdog fires while the relay takes alerts and stops for an hour after one fails": (
        [('alertmanager_notifications_failed_total{integration="webhook"}', "0x10 1x80")],
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
    "an SSH login from elsewhere fires, the PC's login does not": (
        [
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
                "ssh-login-elsewhere",
                posts("ssh-login-elsewhere", "1", user="root", **{"from": "203.0.113.9"}),
            ),
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
    "upkeep that has never run is not due yet, so it never fires": (
        [('up{job="integrations/unix"}', "1x120")],
        [at(70, "upkeep-failed"), at(119, "upkeep-failed")],
    ),
    "a first upkeep that failed fires as no data": (
        [("homelab_upkeep_last_failure_timestamp_seconds", "_x5 60x80")],
        [
            at(64, "upkeep-failed"),
            at(66, "upkeep-failed", posts("upkeep-failed", "-1")),
        ],
    ),
    "a site failing its probe, or its probe not answering, fires after `for`": (
        [
            ('probe_success{job="integrations/blackbox/sites",site="a"}', "1x5 0x20"),
            ('up{job="integrations/blackbox/sites",site="a"}', "1x25"),
            ('probe_success{job="integrations/blackbox/sites",site="b"}', "1x25"),
            ('up{job="integrations/blackbox/sites",site="b"}', "1x25"),
            ('up{job="integrations/blackbox/sites",site="c"}', "1x5 0x20"),
        ],
        [
            at(15, "site-down"),
            at(
                17,
                "site-down",
                posts("site-down", "0", site="a"),
                posts("site-down", "0", site="c"),
            ),
        ],
    ),
    "an unhealthy container fires after `for`, one without a check never": (
        [
            ('container_health_state{job="docker",name="web",id="/a"}', "0x30"),
            ('container_health_state{job="docker",name="db",id="/b"}', "-1x30"),
            ('container_health_state{job="docker",name="api",id="/c"}', "1x30"),
        ],
        [
            at(9, "container-unhealthy"),
            at(11, "container-unhealthy", posts("container-unhealthy", "0", name="web")),
        ],
    ),
    "a service the deploy keeps running fires once no container of it runs, a one-shot never": (
        [
            ('homelab_container_expected{project="app",service="web"}', "1x30"),
            ('homelab_container_expected{project="app",service="db"}', "1x30"),
            (
                'container_last_seen{job="docker",name="app-web-1",container_label_com_docker_compose_oneoff="False",'
                'container_label_com_docker_compose_project="app",container_label_com_docker_compose_service="web"}',
                "1x5",
            ),
            (
                'container_last_seen{job="docker",name="app-db-1",container_label_com_docker_compose_oneoff="False",'
                'container_label_com_docker_compose_project="app",container_label_com_docker_compose_service="db"}',
                "1x30",
            ),
            # A one-shot, not on the deploy's list, that has exited.
            (
                'container_last_seen{job="docker",name="app-once-1",container_label_com_docker_compose_oneoff="False",'
                'container_label_com_docker_compose_project="app",container_label_com_docker_compose_service="once"}',
                "1x2",
            ),
        ],
        [
            # Gone from 6 min, stale after 5 more, then 10 min of `for`.
            at(18, "container-stopped"),
            at(
                22,
                "container-stopped",
                posts("container-stopped", "0", project="app", service="web"),
            ),
        ],
    ),
    # Windows of days, sampled and evaluated hourly or slower, so `for` and each edge hold only to
    # a step; times stay in minutes, on a step.
    # promtool starts at 1 January 1970, 00:00 UTC, so the first due time is 05:00 that day.
    "upkeep enabled before a due time and never run fires 3 days after it, and `for`": (
        [('homelab_timer_last_trigger_timestamp_seconds{timer="backup-upkeep"}', "0x80")],
        [
            at(77 * 60, "upkeep-missed"),
            at(78 * 60, "upkeep-missed", posts("upkeep-missed", "3")),
        ],
        "1h",
    ),
    "a later month's due time counts from the 1st of that month": (
        # Enabled on 15 January, so 1 February 05:00 is the first due time it misses.
        [('homelab_timer_last_trigger_timestamp_seconds{timer="backup-upkeep"}', "1209600x140")],
        [
            # Due 3 days on at 821 h; the first 6-hourly check after it is 822 h, then `for`.
            at(816 * 60, "upkeep-missed"),
            at(822 * 60, "upkeep-missed"),
            at(834 * 60, "upkeep-missed", posts("upkeep-missed", "21")),
        ],
        "6h",
    ),
    "upkeep enabled after the due time, or run since it, stays quiet": (
        [
            ('homelab_timer_last_trigger_timestamp_seconds{timer="backup-upkeep"}', "21600x80"),
            ('homelab_timer_last_trigger_timestamp_seconds{timer="other"}', "0x80"),
        ],
        [at(79 * 60, "upkeep-missed")],
        "1h",
    ),
    "a good run since the due time stays quiet": (
        [
            ('homelab_timer_last_trigger_timestamp_seconds{timer="backup-upkeep"}', "0x80"),
            ("homelab_upkeep_last_success_timestamp_seconds", "_x5 18060x75"),
        ],
        [at(79 * 60, "upkeep-missed")],
        "1h",
    ),
    "a failed run counts as a run, so only upkeep-failed posts": (
        [
            ('homelab_timer_last_trigger_timestamp_seconds{timer="backup-upkeep"}', "0x80"),
            ("homelab_upkeep_last_failure_timestamp_seconds", "_x5 18060x75"),
        ],
        [
            at(79 * 60, "upkeep-missed"),
            at(79 * 60, "upkeep-failed", posts("upkeep-failed", "-1")),
        ],
        "1h",
    ),
    "security updates waiting at every check for 3 days fire, a series younger than that never": (
        [("homelab_security_updates_pending", "2x80")],
        [
            # The offset reaches the first sample at 72 h, then 1 h of `for`.
            at(72 * 60, "security-updates-waiting"),
            at(73 * 60, "security-updates-waiting", posts("security-updates-waiting", "2")),
        ],
        "1h",
    ),
    "security updates installed once in 3 days stay quiet": (
        [("homelab_security_updates_pending", "2x30 0 2x50")],
        [at(75 * 60, "security-updates-waiting")],
        "1h",
    ),
    "a reboot waiting 2 days fires, one done in time never": (
        [("homelab_reboot_required", "1x60")],
        [
            at(48 * 60, "reboot-waiting"),
            at(49 * 60, "reboot-waiting", posts("reboot-waiting", "1")),
        ],
        "1h",
    ),
    "a reboot done within 2 days stays quiet": (
        [("homelab_reboot_required", "1x20 0 1x40")],
        [at(49 * 60, "reboot-waiting")],
        "1h",
    ),
    "metrics stopping is one alert with no labels": (
        [('up{job="integrations/unix"}', "1x5"), ('up{job="docker"}', "1x5")],
        [
            at(5, "beelink-metrics-stopped"),
            at(12, "beelink-metrics-stopped", posts("beelink-metrics-stopped", "1")),
        ],
    ),
    # Hourly: 14 days at 20 s, then 30 hours at 50 s, then back.
    "CI half again and 20 s slower for most of a day fires, and clears once it is fast again": (
        [(CHECK, "20x336 50x30 20x60")],
        [
            at(354 * 60, "ci-slower"),
            at(357 * 60, "ci-slower", posts("ci-slower", "50", **CHECK_LABELS)),
            at(382 * 60, "ci-slower"),
        ],
        "1h",
    ),
    "CI half again slower but under 20 s more stays quiet": (
        [(CHECK, "20x336 35x48")],
        [at(370 * 60, "ci-slower")],
        "1h",
    ),
    "a deploy still failing after half an hour fires, and one fixed sooner does not": (
        [
            ("homelab_main_deploy_behind", "0x5 1x20 0x5 1x40"),
            ("homelab_main_deploy_checked_timestamp_seconds", "0+60x70"),
        ],
        [
            # Behind from 6 to 26 min, then again from 33.
            at(28, "main-not-deployed"),
            at(62, "main-not-deployed"),
            at(64, "main-not-deployed", posts("main-not-deployed", "1")),
        ],
    ),
    "two of a component's loss counters moving together are one post, a drop stage never": (
        [
            (f"otelcol_receiver_refused_spans_total{{{RECEIVER}}}", "0x10 30x30"),
            (f"otelcol_receiver_refused_metric_points_total{{{RECEIVER}}}", "0x10 20x30"),
            (
                'loki_process_dropped_lines_total{job="alloy",component_id="loki.process.noise",'
                'reason="drop_stage"}',
                "0+100x40",
            ),
        ],
        [
            at(10, "telemetry-dropped"),
            # Both rose at 11 min, by 50 together.
            at(12, "telemetry-dropped", posts("telemetry-dropped", "50", component_id=RECEIVER_ID)),
        ],
    ),
    "an exporter's first failure, a counter that starts then, posts and clears": (
        [
            (
                f'otelcol_exporter_queue_capacity{{{EXPORTER},data_type="logs"}}',
                "10000x45",
            ),
            (f"otelcol_exporter_send_failed_log_records_total{{{EXPORTER}}}", "_x10 7x35"),
        ],
        [
            at(9, "telemetry-dropped"),
            at(10, "telemetry-dropped", posts("telemetry-dropped", "7", component_id=EXPORTER_ID)),
            # Out of the 15-minute window at 25, then the 10-minute hold.
            at(37, "telemetry-dropped"),
        ],
    ),
    "a send queue a quarter full fires after `for`, one whose store is down never": (
        [
            (f'otelcol_exporter_queue_capacity{{{EXPORTER},data_type="logs"}}', "10000x30"),
            (f'otelcol_exporter_queue_size{{{EXPORTER},data_type="logs"}}', "0x5 3000x25"),
            (f'otelcol_exporter_queue_capacity{{{TRACES},data_type="traces"}}', "10000x30"),
            (f'otelcol_exporter_queue_size{{{TRACES},data_type="traces"}}', "0x5 5000x25"),
            ('up{job="logs",instance="logs:9428"}', "1x30"),
            ('up{job="traces",instance="traces:10428"}', "0x30"),
        ],
        [
            at(10, "telemetry-backing-up"),
            at(
                11,
                "telemetry-backing-up",
                posts("telemetry-backing-up", "30", component_id=EXPORTER_ID),
            ),
        ],
    ),
    "host-metrics' three jobs gone fires after 10 minutes": (
        [
            ('up{job="caddy",instance="beelink"}', "1x5"),
            ('up{job="coredns",instance="beelink"}', "1x5"),
            ('up{job="homeassistant",instance="beelink"}', "1x5"),
        ],
        [
            at(14, "host-metrics-stopped"),
            at(15, "host-metrics-stopped", posts("host-metrics-stopped", "1")),
        ],
    ),
    "one of host-metrics' jobs still written stays quiet": (
        [
            ('up{job="caddy",instance="beelink"}', "1x5"),
            ('up{job="homeassistant",instance="beelink"}', "0x30"),
        ],
        [at(25, "host-metrics-stopped")],
    ),
    "a unit failing at every run fires after `for`, one failing once never": (
        [
            (
                'homelab_systemd_unit_failed{manager="system",unit="homelab-ci-times.service"}',
                "_x5 " + "1x3 stale " * 6,
            ),
            ('homelab_systemd_unit_failed{manager="user",unit="probe.service"}', "_x5 1x3 stale"),
        ],
        [
            at(19, "unit-failed"),
            at(
                20,
                "unit-failed",
                posts("unit-failed", "1", manager="system", unit="homelab-ci-times.service"),
            ),
        ],
    ),
}


def promql_round(x: float) -> int:
    return math.floor(x + 0.5)


def fresh_cases(slo: slos.Slo) -> dict[str, tuple[Any, ...]]:
    """A writer that stops fires one missed run later and `for`; one that keeps writing never;
    none at all is no data."""
    textfile = slos.TEXTFILE.format(slo.file).replace('=~".*/', '="/var/lib/homelab-metrics/')
    series = slo.metric or textfile
    size, _ = slos.unit(slo.late)
    late = round(slo.late / size, 1)
    first = next(m for m in itertools.count() if promql_round(m * 60 / size) > late)
    wait = slos.seconds(slos.FRESH_FOR) // 60
    end = first + wait
    value = str(promql_round(end * 60 / size))
    return {
        f"{slo.alert}: a writer that stops fires one missed run later": (
            [(series, f"0x{end}")],
            [at(end - 1, slo.alert), at(end, slo.alert, posts(slo.alert, value))],
        ),
        f"{slo.alert}: a writer that keeps writing never fires": (
            [(series, f"0+60x{end}")],
            [at(end, slo.alert)],
        ),
        f"{slo.alert}: no data fires after `for`": (
            [('up{job="docker"}', "1x10")],
            [at(wait - 1, slo.alert), at(wait + 1, slo.alert, posts(slo.alert, "-1"))],
        ),
    }


def outage_cases(slo: slos.Slo) -> dict[str, tuple[Any, ...]]:
    """A good-minutes promise replays a real outage's series (tests/incidents)."""
    outage = json.loads((INCIDENTS / f"{slo.alert}.json").read_text(encoding="utf-8"))
    series = []
    for s in outage["series"]:
        values, seen = [], False
        for v in s["values"]:
            values.append(str(v) if v is not None else "stale" if seen else "_")
            seen = v is not None
        series.append((s["series"], " ".join(values)))
    checks = [at(m, slo.alert, *([posts(slo.alert, v)] if v else [])) for m, v in outage["checks"]]
    return {f"{slo.alert}: its outage in tests/incidents fires": (series, checks)}


INCIDENTS = Path(__file__).parent / "incidents"
PROMISES, _ = slos.catalogue()
for _slo in PROMISES:
    CASES |= fresh_cases(_slo) if _slo.fresh else outage_cases(_slo)


def test_no_rule_reads_another_rules_result() -> None:
    # Each case loads only the rules it checks (rule_runs), which holds only while this does.
    text = "".join(f.read_text(encoding="utf-8") for f in slos.RULE_FILES)
    assert all("alert" in rule for rule in RULES.values())
    assert "record:" not in text and "ALERTS" not in text


@pytest.fixture(scope="module")
def rule_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, subprocess.CompletedProcess[str]]:
    """Every case's promtool result. One promtool per core, each testing its share of the cases:
    one process per case spent half its CPU starting promtool, and one after another they were
    most of CI's time."""
    folder = tmp_path_factory.mktemp("rules")

    def write(numbered: tuple[int, str]) -> str:
        n, case = numbered
        series, checks, *rest = CASES[case]
        step = rest[0] if rest else "1m"
        # Only the rules the case checks: promtool evaluates every rule loaded at every step, and
        # no rule reads another's result (no recording rules, no ALERTS).
        names = {check["alertname"] for check in checks}
        groups = [g | {"rules": [r for r in g["rules"] if r["alert"] in names]} for g in GROUPS]
        rules = folder / f"{n}.rules.yml"
        rules.write_text(
            json.dumps({"groups": [g for g in groups if g["rules"]]}), encoding="utf-8"
        )
        tests = {
            "rule_files": [str(rules)],
            "evaluation_interval": step,
            "tests": [
                {
                    "interval": step,
                    "input_series": [{"series": s, "values": v} for s, v in series],
                    "alert_rule_test": checks,
                }
            ],
        }
        path = folder / f"{n}.yml"
        path.write_text(json.dumps(tests), encoding="utf-8")
        return str(path)

    files = [write(numbered) for numbered in enumerate(CASES)]
    cores = os.cpu_count() or 1
    shares = [share for i in range(cores) if (share := files[i::cores])]
    with ThreadPoolExecutor(len(shares)) as pool:
        runs = list(pool.map(lambda share: promtool("test", "rules", *share), shares))
    # promtool reports each file in turn on stdout ("Processing test file: <file>", then SUCCESS
    # or FAILED) and the failures' detail on stderr, for its whole share.
    status: dict[str, str] = {}
    detail: dict[str, str] = {}
    for share, ran in zip(shares, runs, strict=True):
        for path in share:
            detail[path] = f"stderr of the promtool run with this case and others:\n{ran.stderr}"
        for part in ran.stdout.split("Processing test file: ")[1:]:
            name, _, report = part.partition("\n")
            status[name.strip()] = report
    return {
        case: subprocess.CompletedProcess(
            ["promtool"],
            0 if status.get(path, "").lstrip().startswith("SUCCESS") else 1,
            status.get(path, "no result\n") + detail[path],
            "",
        )
        for case, path in zip(CASES, files, strict=True)
    }


@pytest.mark.parametrize("case", CASES)
def test_rules_fire_hold_and_clear_as_the_standard_says(
    rule_runs: dict[str, subprocess.CompletedProcess[str]], case: str
) -> None:
    ran = rule_runs[case]
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_replay_posts_as_prometheus_and_alertmanager_would() -> None:
    from stacks.observability.prometheus import replay

    # The query returns for 5 h, not for 4 minutes, for 5 h more, then stops.
    returned = {60 * m for m in [*range(0, 300), *range(304, 604)]}
    found = replay.posts(returned, RULES["high-ram"], 0, 60 * 620)
    assert found == [(60 * 5, "firing"), (60 * 613, "cleared")]
