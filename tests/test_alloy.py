"""Alloy (stacks/observability/config.alloy) keeps every metric and the container labels read, and
loads (its validate command)."""

import re
import subprocess
import threading
from pathlib import Path

import yaml

from tests.conftest import tool

ROOT = Path(__file__).parent.parent
STACK = ROOT / "stacks/observability"
CONFIG = (STACK / "config.alloy").read_text(encoding="utf-8")
READERS = (*sorted((STACK / "grafana/dashboards").glob("*.json")), STACK / "public_stats.py")


def queries() -> list[str]:
    """Every alert and dashboard query, and the scripts that read metrics."""
    rules = yaml.safe_load((STACK / "prometheus/rules.yml").read_text(encoding="utf-8"))
    texts = [str(rule["expr"]) for group in rules["groups"] for rule in group["rules"]]
    return texts + [reader.read_text(encoding="utf-8") for reader in READERS]


def test_no_metric_is_dropped_by_name() -> None:
    # The store keeps what a later fix may need, so no rule filters on the metric name.
    assert not re.search(r'source_labels\s*=\s*\[[^\]]*"__name__"', CONFIG)
    # Nor do the host and container exporters narrow their default sets.
    assert "set_collectors" not in CONFIG
    docker = CONFIG[CONFIG.index('prometheus.exporter.cadvisor "docker"') :]
    assert "enabled_metrics" not in docker[: docker.index("\n}")]


def test_every_container_label_read_is_sent() -> None:
    block = CONFIG[CONFIG.index("allowlisted_container_labels") :]
    allowed = re.findall(r'"([^"]+)"', block[: block.index("]")])
    sent = {"container_label_" + re.sub(r"[.-]", "_", label) for label in allowed}
    read = {name for text in queries() for name in re.findall(r"\bcontainer_label_\w+", text)}
    assert "container_label_com_docker_compose_project" in read
    assert read <= sent


def test_the_config_loads() -> None:
    command = [tool("alloy"), "validate", str(STACK / "config.alloy")]
    ran = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, ran.stdout + ran.stderr


# A line in each format the containers write, a word found only in it, and the level it gets.
LINES = [
    ('ts=2026-11-13T13:35:30Z level=info msg="ready"', "ready", "info"),
    ('t=2026-11-13T13:35:11Z level=warn msg="manifest watcher"', "manifest", "warn"),
    ('time=2026-11-12T22:36:18Z level=ERROR msg="Probe failed"', "Probe", "error"),
    ("2026-11-13T13:27:28Z INF precheck complete", "precheck", "info"),
    ("2026-11-13T13:35:21.384Z\twarn\tVictoriaTraces/app/logsql.go:1178\tslow", "logsql", "warn"),
    ('INFO:     192.168.96.2:55072 - "GET /aircraft HTTP/1.1" 200 OK', "aircraft", "info"),
    ("ERROR: cannot ring: timed out", "ring", "error"),
    (
        '{"time": "2026-11-13T13:35:30.000+00:00", "level": "ERROR", "msg": "cannot post"}',
        "post",
        "error",
    ),
    ("\x1b[31m2026-11-12 20:25:47.122 ERROR (MainThread) [habluetooth]", "habluetooth", "error"),
    ("2026-11-12 20:25:44.669 CRITICAL (MainThread) [homeassistant]", "homeassistant", "fatal"),
    ('{"level":"warn","logger":"http.log","msg":"handled request"}', "handled", "warn"),
    ('{"level":"INFO","msg":"saw level=error in a reply"}', "reply", "info"),
    ("Error fetching from ADS-B API, trying fallback", "ADS-B", ""),
]
# Lines from these containers: the first of each is dropped, the second kept.
NOISE = {
    "observability-docker-socket-proxy-1": [
        "::ffff:172.29.0.4:48868 [13/Nov/2026:13:35:21.154] proxy docker/socket 0/0/0/1/1 200 9092"
        ' - - ---- 27/27/23/23/0 0/0 "GET /v1.56/containers/9da7/json HTTP/1.1"',
        "::ffff:172.29.0.4:48868 [13/Nov/2026:13:35:21.169] proxy docker/socket 0/0/0/1/1 403 212"
        ' - - PR-- 27/27/23/23/0 0/0 "POST /v1.56/containers/9da7/stop HTTP/1.1"',
    ],
    "observability-alloy-1": [
        'ts=2026-11-13T13:35:30Z level=info msg="Failed to update stats: unable to determine device'
        ' info for dir: /rootfs/var/lib/docker/overlay2/ddf0/diff: stat failed"',
        'ts=2026-11-14T08:00:00Z level=info msg="reload requested"',
    ],
}


def test_each_log_format_gets_one_level_and_noise_is_dropped(tmp_path: Path) -> None:
    alloy = tool("alloy")
    starts = [CONFIG[CONFIG.index(f'loki.process "{name}"') :] for name in ("noise", "docker")]
    block = "".join(start[: start.index("\n}\n") + 3] for start in starts)
    # Without the secret filter between them, which needs no testing here.
    block = block.replace("loki.secretfilter.docker.receiver", "loki.process.docker.receiver")
    block = block.replace("otelcol.receiver.loki.logs.receiver", "loki.echo.out.receiver")
    files = {"app": [line for line, _, _ in LINES], **NOISE}
    targets = []
    for name, lines in files.items():
        path = tmp_path / f"{name}.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        targets.append(f'{{__path__ = "{path.as_posix()}", container_name = "{name}"}}')
    (tmp_path / "test.alloy").write_text(
        f'local.file_match "s" {{\n  path_targets = [{", ".join(targets)}]\n}}\n'
        'loki.source.file "s" {\n  targets = local.file_match.s.targets\n'
        "  forward_to = [loki.process.noise.receiver]\n  tail_from_end = false\n}\n"
        f'{block}\nloki.echo "out" {{}}\n',
        encoding="utf-8",
    )
    command = [alloy, "run", f"--storage.path={tmp_path / 'data'}", str(tmp_path / "test.alloy")]
    seen: list[str] = []
    with subprocess.Popen(command, stderr=subprocess.PIPE, text=True, encoding="utf-8") as run:
        assert run.stderr is not None
        # A line wrongly dropped would leave the loop waiting for it: fail instead.
        deadline = threading.Timer(60, run.kill)
        deadline.start()
        try:
            for out in run.stderr:
                if "component_id=loki.echo.out" in out:
                    seen.append(out)
                if len(seen) == len(LINES) + 2:
                    break
        finally:
            deadline.cancel()
            run.kill()
    for line, word, level in LINES:
        got = [re.findall(r'level=\\"(\w+)\\"', out) for out in seen if word in out]
        assert got == [[level] if level else []], line
    kept = "".join(seen)
    assert "PR--" in kept and "reload requested" in kept
    assert " 200 9092" not in kept and "device" not in kept
