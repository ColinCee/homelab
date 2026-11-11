"""The observability stack's networks keep writers, readers and Alertmanager apart, and Grafana
asks for a login (stacks/observability/compose.yaml)."""

from pathlib import Path

import yaml

STACK = Path(__file__).parent.parent / "stacks/observability"
COMPOSE = yaml.safe_load((STACK / "compose.yaml").read_text(encoding="utf-8"))
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


def test_grafana_asks_for_a_login_and_fetches_nothing_at_start() -> None:
    env = SERVICES["grafana"]["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert "GF_USERS_VIEWERS_CAN_EDIT" not in env
    assert env["GF_SERVER_ENFORCE_DOMAIN"] == "true"
    assert env["GF_SECURITY_SECRET_KEY__FILE"] == "/run/secrets/grafana_secret_key"
    assert "GF_PLUGINS_PREINSTALL_SYNC" not in env
    dockerfile = (STACK / "grafana.Dockerfile").read_text(encoding="utf-8")
    assert "ADD --checksum=sha256:" in dockerfile


def test_one_heavy_query_cannot_take_prometheus_memory() -> None:
    flags = SERVICES["prometheus"]["command"]
    assert any(f.startswith("--query.max-samples=") for f in flags)
    assert any(f.startswith("--query.timeout=") for f in flags)
    for store in ("logs/victorialogs", "traces/victoriatraces"):
        spec = yaml.safe_load((STACK / store / "compose.yaml").read_text(encoding="utf-8"))
        command = next(iter(spec["services"].values()))["command"]
        assert "-search.maxConcurrentRequests=2" in command, store


def test_openobserve_history_starts_only_by_hand_and_on_loopback() -> None:
    history = SERVICES["openobserve-history"]
    assert history["profiles"] == ["history"]
    assert all(port.startswith("127.0.0.1:") for port in history["ports"])
    assert int(history["environment"]["ZO_COMPACT_DATA_RETENTION_DAYS"]) > 365


def test_a_log_line_opens_its_trace_in_either_log_store() -> None:
    for store in ("loki", "victorialogs"):
        provisioned = yaml.safe_load(
            (STACK / "logs" / store / "datasource.yaml").read_text(encoding="utf-8")
        )
        [field] = provisioned["datasources"][0]["jsonData"]["derivedFields"]
        assert field["matcherRegex"] == "trace_id" and field["datasourceUid"] == "traces", store
        # `$$` keeps provisioning from expanding Grafana's own variable.
        assert field["url"] == "$${__value.raw}", store


def test_obi_traces_every_container_only_when_turned_on() -> None:
    obi = SERVICES["obi"]
    assert obi["profiles"] == ["obi"] and obi["privileged"] is True
    # Its Go tracer failed to make its maps under 320 MiB.
    assert int(obi["mem_limit"].removesuffix("m")) >= 448
    config = yaml.safe_load((STACK / "obi/obi.yaml").read_text(encoding="utf-8"))
    assert config["discovery"]["instrument"] == [{"containers_only": True, "exe_path": "*"}]


def test_tempo_caps_its_searches() -> None:
    config = yaml.safe_load((STACK / "traces/tempo/tempo.yaml").read_text(encoding="utf-8"))
    assert config["querier"]["max_concurrent_queries"] <= 4
    assert config["query_frontend"]["search"]["max_result_limit"] <= 20000


def test_the_trial_stores_into_the_volumes_the_live_stack_will_use() -> None:
    class Loader(yaml.SafeLoader):
        pass

    # Compose's own tag for "take nothing from the file below".
    Loader.add_constructor("!reset", lambda loader, node: [])
    trial = yaml.load((STACK / "trial.yaml").read_text(encoding="utf-8"), Loader=Loader)
    stores = {
        "prometheus-data",
        "loki-data",
        "victorialogs-data",
        "tempo-data",
        "victoriatraces-data",
    }
    assert {k: v["name"] for k, v in trial["volumes"].items()} == {
        k: f"observability_{k}" for k in stores
    }
    for service in trial["services"].values():
        assert "ports" not in service or service["ports"] == []


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
