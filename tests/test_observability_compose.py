"""The observability stack's networks keep writers, readers and Alertmanager apart, no container
with a host folder can read the host's secrets, and nothing opens a way in without a login
(stacks/observability/compose.yaml)."""

import tomllib
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
    # Prometheus also scrapes the flight tracker's probe, and takes host-metrics' writes from the
    # host on a network of its own.
    for name in ("prometheus", "logs", "traces"):
        allowed = {"ingest", "stores", "alerting", "probe", "host-writes"}
        assert set(SERVICES[name]["networks"]) <= allowed, name
    assert members("host-writes") == {"prometheus"}
    assert COMPOSE["networks"]["host-writes"]["internal"] is True


def test_only_prometheus_and_the_gateway_reach_alertmanager() -> None:
    assert members("alerting") == {"prometheus", "alertmanager", "gateway"}
    assert set(SERVICES["alertmanager"]["networks"]) == {
        "alerting",
        "alertmanager-out",
        "incidents",
    }
    assert members("alertmanager-out") == {"alertmanager"}


def test_only_alertmanager_reaches_the_incidents_relay() -> None:
    # Whatever reaches it can open posts and start the on-call agent.
    assert members("incidents") == {"alertmanager", "incidents"}
    assert COMPOSE["networks"]["incidents"]["internal"] is True
    assert set(SERVICES["incidents"]["networks"]) == {"incidents", "incidents-out"}
    assert members("incidents-out") == {"incidents"}
    assert "ports" not in SERVICES["incidents"]


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


def test_the_gateway_runs_read_only_as_nobody_with_no_capabilities() -> None:
    # The gateway faces every reader; tests/test_gateway.py runs its Caddy outside the container.
    gateway = SERVICES["gateway"]
    assert gateway["read_only"] is True and gateway["user"] == "65534:65534"
    assert gateway["cap_drop"] == ["ALL"] and gateway["security_opt"] == ["no-new-privileges:true"]
    assert [t.split(":")[0] for t in gateway["tmpfs"]] == ["/data", "/config"]


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


def test_nothing_reads_or_changes_the_stack_without_a_login() -> None:
    grafana = SERVICES["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    # A snapshot and a public dashboard are each read by its link alone.
    assert grafana["GF_SNAPSHOTS_ENABLED"] == grafana["GF_PUBLIC_DASHBOARDS_ENABLED"] == "false"
    assert "GF_USERS_VIEWERS_CAN_EDIT" not in grafana
    flags = SERVICES["prometheus"]["command"]
    assert not [f for f in flags if "admin" in f or "lifecycle" in f]


def test_the_tests_tools_are_the_versions_the_stack_runs() -> None:
    # A rule or config the tests pass could otherwise fail in the running stack.
    tools = tomllib.loads((Path(__file__).parent.parent / "mise.toml").read_text(encoding="utf-8"))
    for service, name in [
        ("prometheus", "aqua:prometheus/prometheus"),
        ("alloy", "aqua:grafana/alloy"),
        ("gateway", "aqua:caddyserver/caddy"),
    ]:
        image = SERVICES[service]["image"]
        tag = image.split("@")[0].rsplit(":", 1)[1].removeprefix("v")
        assert tools["tools"][name] == tag, f"mise.toml {name} is not compose.yaml's {image}"
