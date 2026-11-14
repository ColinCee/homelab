"""Each thing the lab runs has one name in every store (docs/architecture.md, "Monitoring and
alerts"): a container its Compose service, a host job its systemd unit without `.service`, as
Alloy files their lines. Every name a program sends, a scrape files under or a dashboard or rule
selects must be one of them."""

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

from conftest import ROOT, load_yaml, programs, services, tracked, units

OBSERVABILITY = ROOT / "stacks" / "observability"
# Its comments quote other labels.
ALLOY = "\n".join(
    line
    for line in (OBSERVABILITY / "config.alloy").read_text(encoding="utf-8").splitlines()
    if not line.lstrip().startswith("//")
)

# Jobs that are no one thing, each series naming its own.
NOT_ONE_THING = {
    # The host: node_exporter, and the textfiles host jobs write, labelled by `timer` or `unit`.
    "integrations/unix",
    # cAdvisor over every container, each by `container_label_com_docker_compose_service`.
    "docker",
    # Public sites probed through Cloudflare, outside the lab.
    "integrations/blackbox/flight-tracker",
    "integrations/blackbox/sites",
}
# A CI run's levels (ci-runner/run-times/ci_times.py): GitHub's runs, not the lab's, each level a
# service so Grafana's timeline colours it apart.
CI_LEVELS = {"ci", "ci job", "ci step", "ci task", "ci test"}


def names() -> set[str]:
    """Every Compose service and host unit, as Alloy names their lines (loki.relabel "journal")."""
    found = {name for _, name, _ in services()}
    # The CI VM image's own units run inside a VM; a template unit is no service.
    found |= {u.stem for u in units() if "rootfs" not in u.parts and "@" not in u.stem}
    # Units started at run time, one per VM, filed under their shared prefix.
    found |= set(re.findall(r'regex\s*=\s*"\(([\w-]+)\)-\.\+', ALLOY))
    return found


def sent() -> Iterator[tuple[Path, str]]:
    """The service names programs send traces under: `telemetry.run`'s, and each span's beside
    `telemetry.otlp_span`."""
    for path in programs():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr) == ("telemetry", "run")
            ):
                [first] = [*node.args[:1], *(k.value for k in node.keywords if k.arg == "service")]
            elif (
                isinstance(node, ast.Tuple)
                and len(node.elts) == 2
                and isinstance(node.elts[1], ast.Call)
                and isinstance(node.elts[1].func, ast.Attribute)
                and node.elts[1].func.attr == "otlp_span"
            ):
                first = node.elts[0]
            else:
                continue
            assert isinstance(first, ast.Constant), f"{path.name}:{node.lineno}: name it literally"
            yield path, str(first.value)


JOBS = (
    r'job_name\s*=\s*"([^"]+)"',
    r'target_label\s*=\s*"job"\s*\n\s*replacement\s*=\s*"([^"]+)"',
)


def scraped() -> Iterator[tuple[str, str]]:
    """The jobs Alloy and Prometheus file scrapes under."""
    for pattern in JOBS:
        yield from (("config.alloy", str(m.group(1))) for m in re.finditer(pattern, ALLOY))
    for path in tracked("prometheus.yml"):
        config = load_yaml(path)
        assert isinstance(config, dict), path
        for scrape in config.get("scrape_configs", []):
            yield path.name, str(scrape["job_name"])


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


# PromQL's and Alloy's `=`, `!=`, `=~`, `!~`, and LogsQL's `:`, `:=`, `:!=`.
SELECTORS = (
    r'\bjob\s*!?[=~]~?\s*"([^"]*)"',
    r'\bservice_name\s*(?:!?[=~]~?|:[!=]{0,2})\s*"?([\w./|-]+)',
    r'"?resource_attr:service\.name"?\s*:[!=]{0,2}\s*"?([\w./|-]+)',
)


def selected() -> Iterator[tuple[str, str]]:
    """The names dashboards, rules and Alloy's own matches select, but for regexes past `a|b`."""
    texts = [("config.alloy", ALLOY)]
    for path in tracked(".yml"):
        if path.parent.name == "prometheus":
            texts.append((path.name, path.read_text(encoding="utf-8")))
    for path in (OBSERVABILITY / "grafana" / "dashboards").glob("*.json"):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        texts += [(path.name, text) for text in strings(dashboard)]
    for where, text in texts:
        for pattern in SELECTORS:
            for match in re.finditer(pattern, text):
                for name in str(match.group(1)).split("|"):
                    if name and not re.search(r"[\\^$.*+?()\[\]{}]", name.replace("/", "")):
                        yield where, name


def test_programs_send_traces_under_the_name_their_logs_carry() -> None:
    known = names() | CI_LEVELS
    wrong = {f"{path.name}: {name}" for path, name in sent() if name not in known}
    assert not wrong, f"not a Compose service or host unit: {sorted(wrong)}"


def test_scrape_jobs_are_named_for_what_they_scrape() -> None:
    known = names() | NOT_ONE_THING
    wrong = {f"{where}: {job}" for where, job in scraped() if job not in known}
    assert not wrong, f"not a Compose service or host unit: {sorted(wrong)}"


def test_dashboards_and_rules_select_names_the_stores_hold() -> None:
    known = names() | NOT_ONE_THING | CI_LEVELS
    wrong = {f"{where}: {name}" for where, name in selected() if name not in known}
    assert not wrong, f"no store files anything under: {sorted(wrong)}"


def test_the_check_reads_every_route() -> None:
    # A route whose pattern stopped matching would let every name through.
    assert {"deploy-main", "backup-upkeep", "public-stats"} <= {name for _, name in sent()}
    assert {"alloy", "homelab-ci-vm", "proxy", "dns", "prometheus"} <= {j for _, j in scraped()}
    assert {"deploy-main", "logs", "proxy", "cloudflared"} <= {n for _, n in selected()}
