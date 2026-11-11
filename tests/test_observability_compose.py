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
    for name in ("prometheus", "logs", "traces"):
        assert set(SERVICES[name]["networks"]) <= {"ingest", "stores", "alerting"}, name


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
    assert env["GF_SECURITY_SECRET_KEY"].startswith("${")
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
