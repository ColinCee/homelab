"""The observability stack's networks keep writers, readers and Alertmanager apart, and no
container with a host folder can read the host's secrets (stacks/observability/compose.yaml)."""

from pathlib import Path

import yaml

COMPOSE = yaml.safe_load(
    (Path(__file__).parent.parent / "stacks/observability/compose.yaml").read_text(encoding="utf-8")
)
SERVICES = COMPOSE["services"]


def members(network: str) -> set[str]:
    return {name for name, spec in SERVICES.items() if network in spec.get("networks", [])}


def test_only_alloy_reaches_the_stores_write_paths() -> None:
    assert members("ingest") == {"alloy", "prometheus", "logs", "traces"}
    assert members("stores") == {"gateway", "prometheus", "logs", "traces"}
    # The gateway reads; nothing but the stores and it, and Alloy, shares a network with a store.
    # Prometheus also scrapes the flight tracker's probe.
    for name in ("prometheus", "logs", "traces"):
        assert set(SERVICES[name]["networks"]) <= {"ingest", "stores", "alerting", "probe"}, name


def test_only_prometheus_and_the_gateway_reach_alertmanager() -> None:
    assert members("alerting") == {"prometheus", "alertmanager", "gateway"}
    assert set(SERVICES["alertmanager"]["networks"]) == {"alerting", "alertmanager-out"}
    assert members("alertmanager-out") == {"alertmanager"}


def test_programs_that_send_traces_reach_alloy_alone() -> None:
    assert members("otlp") == {"alloy", "obi"}
    assert COMPOSE["networks"]["otlp"]["internal"] is True


def test_grafana_reaches_only_the_gateway() -> None:
    assert SERVICES["grafana"]["networks"] == ["grafana"]
    assert members("grafana") == {"grafana", "grafana-users", "gateway"}


def test_store_networks_reach_no_internet() -> None:
    for network in ("ingest", "stores", "alerting", "docker-api"):
        assert COMPOSE["networks"][network]["internal"] is True, network


def test_no_container_with_a_host_folder_can_read_a_secret_on_the_host() -> None:
    # Not root, no capabilities, and only the folders it reads: never the host's root, home or
    # Docker's data. OBI is privileged, and runs only when its profile is on.
    closed = ("/", "/etc", "/home", "/root", "/var/lib/docker")
    for name, spec in SERVICES.items():
        host = [v.split(":")[0] for v in spec.get("volumes", []) if v.startswith("/")]
        if not host or name == "obi":
            continue
        assert spec["user"].split(":")[0] not in ("0", "root"), name
        assert spec["cap_drop"] == ["ALL"], name
        assert "cap_add" not in spec and "privileged" not in spec, name
        assert not [p for p in host if p in closed or p.startswith(("/home/", "/root/"))], name


def test_alloy_reaches_no_internet_host_or_tailnet() -> None:
    alloy = SERVICES["alloy"]
    assert all(COMPOSE["networks"][n]["internal"] is True for n in alloy["networks"])
    assert "extra_hosts" not in alloy and "network_mode" not in alloy
    # The flight tracker's probe reaches the internet and probes any address, so only Prometheus
    # reaches it, and it has no host folder.
    assert SERVICES["blackbox"]["networks"] == ["default", "probe"]
    assert (
        members("probe") == {"prometheus", "blackbox"} and COMPOSE["networks"]["probe"]["internal"]
    )
    assert not [v for v in SERVICES["blackbox"]["volumes"] if v.startswith("/")]
