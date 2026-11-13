"""Alloy (stacks/observability/config.alloy) keeps every metric and the container labels read, and
loads (its validate command)."""

import ast
import json
import re
import socket
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any

from conftest import ROOT, tool
from stacks.observability.prometheus import slos

STACK = ROOT / "stacks/observability"
CONFIG = (STACK / "config.alloy").read_text(encoding="utf-8")
READERS = (
    *sorted((STACK / "grafana/dashboards").glob("*.json")),
    STACK / "public-stats/public_stats.py",
)


def queries() -> list[str]:
    """Every alert and dashboard query, and the scripts that read metrics."""
    texts = [str(rule["expr"]) for group in slos.rule_groups() for rule in group["rules"]]
    return texts + [reader.read_text(encoding="utf-8") for reader in READERS]


def test_no_metric_is_dropped_by_name() -> None:
    # The store keeps what a later fix may need, so no rule filters on the metric name.
    assert not re.search(r'source_labels\s*=\s*\[[^\]]*"__name__"', CONFIG)
    # Nor do the host and container exporters narrow their default sets.
    assert "set_collectors" not in CONFIG
    docker = CONFIG[CONFIG.index('prometheus.exporter.cadvisor "docker"') :]
    docker = docker[: docker.index("\n}")]
    assert "enabled_metrics" not in docker
    # But for disk usage, which it cannot read: cAdvisor's default set as Alloy has it
    # (internal/static/integrations/cadvisor/cadvisor.go).
    disabled = re.search(r"disabled_metrics\s*=\s*\[([^\]]*)\]", docker)
    assert disabled
    assert set(re.findall(r'"(\w+)"', disabled.group(1))) == {
        *("advtcp", "cpu_topology", "cpuset", "hugetlb", "memory_numa", "process"),
        *("referenced_memory", "resctrl", "sched", "tcp", "udp", "disk"),
    }
    read = "".join(queries())
    assert not re.search(r"container_fs_(usage|limit|inodes)", read)


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
    # Prometheus, Alertmanager, blackbox and OBI (Go's slog)
    (
        '{"level":"ERROR","source":"main.go:774","msg":"Error loading"}',
        "loading",
        "error",
    ),
    (
        '{"time":"2026-11-13T14:41:49Z","level":"INFO","msg":"OpenTelemetry eBPF Instrumentation"}',
        "eBPF",
        "info",
    ),
    # VictoriaLogs and VictoriaTraces
    (
        '{"ts":"2026-11-13T14:41:56Z","level":"warn","caller":"lib/flag.go:12","msg":"slow query"}',
        "slow",
        "warn",
    ),
    # Grafana
    (
        '{"level":"warn","logger":"settings","msg":"Deprecation Notice"}',
        "Deprecation",
        "warn",
    ),
    # cloudflared (zerolog)
    (
        '{"level":"error","time":"2026-11-13T14:42:00Z","message":"Connection terminated"}',
        "terminated",
        "error",
    ),
    # Alloy
    ('{"ts":"2026-11-13T14:41:43Z","level":"info","msg":"Alloy is starting"}', "starting", "info"),
    # Our services
    (
        '{"time":"2026-11-13T13:35:30.000+00:00","level":"ERROR","msg":"cannot post"}',
        "post",
        "error",
    ),
    # Caddy
    ('{"level":"warn","logger":"http.log","msg":"handled request"}', "handled", "warn"),
    ('{"level":"INFO","msg":"saw level=error in a reply"}', "reply", "info"),
    # CoreDNS and Home Assistant, which cannot log JSON
    ("[INFO] plugin/reload: Running configuration SHA512", "SHA512", "info"),
    ("\x1b[31m2026-11-12 20:25:47.122 ERROR (MainThread) [habluetooth]", "habluetooth", "error"),
    ("2026-11-12 20:25:44.669 CRITICAL (MainThread) [homeassistant]", "homeassistant", "fatal"),
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
        '{"ts":"2026-11-13T13:35:30Z","level":"info","msg":"Failed to update stats for container'
        ' \\"/system.slice/docker-73fa.scope\\": unable to determine device info for dir:'
        ' /rootfs/var/lib/docker/overlay2/ddf0/diff: stat failed"}',
        '{"ts":"2026-11-14T08:00:00Z","level":"info","msg":"reload requested"}',
    ],
}
# Host lines by the journal's fields, a word found only in each, and the service it is filed under.
JOURNAL = [
    (
        {"_systemd_unit": "init.scope", "syslog_identifier": "systemd"},
        "Started homelab-ci-update.service.",
        "Started",
        "systemd",
    ),
    # A host script's line through telemetry.
    (
        {"_systemd_unit": "homelab-ci-times.service", "syslog_identifier": "python3"},
        '{"time":"2026-11-13T13:35:30.000+00:00","level":"WARNING","logger":"ci-times",'
        '"msg":"skipped a run"}',
        "skipped",
        "homelab-ci-times",
    ),
    ({"syslog_identifier": "kernel"}, "tun: Universal TUN/TAP driver", "TUN/TAP", "kernel"),
    (
        {"_systemd_unit": "homelab-ci-vm-fc-0-1794595639.service", "syslog_identifier": "x"},
        "losetup: attached the VM's image",
        "losetup",
        "homelab-ci-vm",
    ),
    (
        {
            "_systemd_unit": "user@1000.service",
            "_systemd_user_unit": "deploy-main.service",
            "syslog_identifier": "python3",
        },
        "deploy found nothing new",
        "nothing new",
        "deploy-main",
    ),
    # The user manager's own line, not a service's: a template unit is no service name.
    (
        {"_systemd_unit": "user@1000.service", "syslog_identifier": "systemd"},
        "Reached target timers.target",
        "Reached",
        "systemd",
    ),
    ({}, "a line with no field to name it", "no field", "unknown_service"),
]


def test_each_log_format_gets_one_level_and_noise_is_dropped(tmp_path: Path) -> None:
    alloy = tool("alloy")
    process = block("loki.process", "noise") + block("loki.process", "docker")
    # Without the secret filter between them, which needs no testing here.
    process = process.replace("loki.secretfilter.docker.receiver", "loki.process.docker.receiver")
    process = process.replace("otelcol.receiver.loki.logs.receiver", "loki.echo.out.receiver")
    # The journal's rules, fed by files: a file target's labels lose their `__`, so the journal's
    # fields go without it.
    relabel = block("loki.relabel", "journal").replace("__journal_", "journal_")
    relabel = relabel.replace("forward_to = []", "forward_to = [loki.process.docker.receiver]")
    files = {"app": [line for line, _, _ in LINES], **NOISE}
    targets = []
    for name, lines in files.items():
        path = tmp_path / f"{name}.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        targets.append(f'{{__path__ = "{path.as_posix()}", container_name = "{name}"}}')
    journal = []
    for i, (fields, line, _, _) in enumerate(JOURNAL):
        path = tmp_path / f"journal{i}.log"
        path.write_text(line + "\n", encoding="utf-8")
        labels = "".join(f', journal_{key} = "{value}"' for key, value in fields.items())
        journal.append(f'{{__path__ = "{path.as_posix()}", container_name = "journal"{labels}}}')
    (tmp_path / "test.alloy").write_text(
        f'local.file_match "s" {{\n  path_targets = [{", ".join(targets)}]\n}}\n'
        'loki.source.file "s" {\n  targets = local.file_match.s.targets\n'
        "  forward_to = [loki.process.noise.receiver]\n  tail_from_end = false\n}\n"
        f'local.file_match "j" {{\n  path_targets = [{", ".join(journal)}]\n}}\n'
        'loki.source.file "j" {\n  targets = local.file_match.j.targets\n'
        "  forward_to = [loki.relabel.journal.receiver]\n  tail_from_end = false\n}\n"
        f'{relabel}{process}\nloki.echo "out" {{}}\n',
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
                if len(seen) == len(LINES) + 2 + len(JOURNAL):
                    break
        finally:
            deadline.cancel()
            run.kill()
    for line, word, level in LINES:
        got = [re.findall(r'level=\\"(\w+)\\"', out) for out in seen if word in out]
        assert got == [[level] if level else []], line
    # Every line leaves as one JSON object, whatever its program wrote.
    for out in seen:
        entry = re.search(r' entry=("(?:[^"\\]|\\.)*")', out)
        assert entry, out
        assert isinstance(json.loads(ast.literal_eval(entry.group(1))), dict), out
    kept = "".join(seen)
    assert "PR--" in kept and "reload requested" in kept and "homelab-ci-update" in kept
    host = [re.findall(r'level=\\"(\w+)\\"', out) for out in seen if "skipped a run" in out]
    assert host == [["warn"]]
    assert " 200 9092" not in kept and "device" not in kept
    # Every host line is filed under a service, as a container's is; the files arrive in any order.
    for _, line, word, service in JOURNAL:
        got = [re.findall(r'service_name=\\"([\w-]+)\\"', out) for out in seen if word in out]
        assert got == [[service]], line


def block(kind: str, label: str) -> str:
    start = CONFIG[CONFIG.index(f'\n{kind} "{label}" {{') + 1 :]
    return start[: start.index("\n}\n") + 3]


def test_each_call_is_named_by_the_container_it_reached(tmp_path: Path) -> None:
    alloy = tool("alloy")
    # Containers as Docker's API lists them: one reached on its network, one at the host port it
    # publishes, one on the host's network; a label's text that looks like the printed targets'
    # own must not leak in.
    service = "__meta_docker_container_label_com_docker_compose_service"
    containers = (
        f'[{{__meta_docker_network_ip = "172.31.0.3", {service} = "prometheus", '
        '__meta_docker_container_name = "/a\\"=\\"b} {"}, '
        '{__meta_docker_network_ip = "172.26.0.3", '
        '__meta_docker_port_public_ip = "100.100.146.119", __meta_docker_port_public = "5080", '
        f'{service} = "grafana"}}, {{__meta_docker_container_network_mode = "host", '
        f'__meta_docker_container_name = "/observability-proxy-1", {service} = "proxy"}}]'
    )
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    peers = block("discovery.relabel", "peers").replace(
        "discovery.docker.peers.targets", containers
    )
    transform = block("otelcol.processor.transform", "peers").replace(
        "otelcol.processor.batch.traces.input", "otelcol.exporter.debug.out.input"
    )
    (tmp_path / "test.alloy").write_text(
        f'otelcol.receiver.otlp "in" {{\n  http {{\n    endpoint = "127.0.0.1:{port}"\n  }}\n'
        "  output {\n    traces = [otelcol.processor.transform.peers.input]\n  }\n}\n"
        f'{peers}{transform}otelcol.exporter.debug "out" {{\n  verbosity = "detailed"\n}}\n',
        encoding="utf-8",
    )

    def span(
        span_id: str, address: str, server_port: int, connected: bool = True
    ) -> dict[str, Any]:
        # A call that never connected has only the address it was sent to.
        key = "network.peer.address" if connected else "server.address"
        attributes = {key: {"stringValue": address}, "server.port": {"intValue": server_port}}
        return {
            "traceId": "0" * 31 + "1",
            "spanId": span_id,
            "name": span_id,
            "kind": 3,
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "attributes": [{"key": k, "value": v} for k, v in attributes.items()],
        }

    spans = [
        span("000000000000000a", "172.31.0.3", 9090),
        span("000000000000000b", "100.100.146.119", 5080),
        span("000000000000000c", "100.100.146.119", 80),
        span("000000000000000d", "172.31.0.3", 9090, connected=False),
    ]
    senders = [
        {"key": "container.name", "value": {"stringValue": name}}
        for name in ("observability-proxy-1", "observability-grafana-1")
    ]
    body = json.dumps(
        {
            "resourceSpans": [
                {"resource": {"attributes": [sender]}, "scopeSpans": [{"spans": spans[i::2]}]}
                for i, sender in enumerate(senders)
            ]
        }
    ).encode()
    # The debug exporter is experimental; test_the_config_loads holds the live config to GA.
    data = f"--storage.path={tmp_path / 'data'}"
    command = [alloy, "run", "--stability.level=experimental", data, str(tmp_path / "test.alloy")]
    printed = ""
    with subprocess.Popen(command, stderr=subprocess.PIPE, text=True, encoding="utf-8") as run:
        assert run.stderr is not None
        deadline = threading.Timer(60, run.kill)
        deadline.start()
        try:
            for out in run.stderr:
                if "Starting HTTP server" in out and "otelcol.receiver.otlp.in" in out:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/traces",
                        body,
                        {"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(request, timeout=10).close()
                if "component_id=otelcol.exporter.debug.out" in out and "Span #0" in out:
                    printed += out
                    if printed.count("ResourceSpans #") == len(senders):
                        break
        finally:
            deadline.cancel()
            run.kill()
    printed = printed.replace("\\n", "\n")
    named = re.findall(r"Name +: (\w+).*?Attributes:(.*?)(?=Span #|ResourceSpans|$)", printed, re.S)
    assert {
        span_id[-1]: re.findall(r"peer.service: Str\((\w+)\)", attrs) for span_id, attrs in named
    } == {
        "a": ["prometheus"],
        "b": ["grafana"],
        "c": [],
        "d": ["prometheus"],
    }
    marked = re.findall(r"container.name: Str\(([\w-]+)\)\n.*?container.network.mode", printed)
    assert marked == ["observability-proxy-1"]


def test_container_logs_carry_their_service_name() -> None:
    # The rules must be in the block that the log source reads, not another relabel block.
    source = CONFIG[CONFIG.index('loki.source.docker "containers"') :]
    rules = re.search(r"relabel_rules\s*=\s*discovery\.relabel\.(\w+)\.rules", source)
    assert rules
    block = CONFIG[CONFIG.index(f'discovery.relabel "{rules.group(1)}"') :]
    block = block[: block.index("\n}\n")]
    assert '"__meta_docker_container_label_com_docker_compose_service"' in block
    assert re.search(r'target_label\s*=\s*"service_name"', block)
