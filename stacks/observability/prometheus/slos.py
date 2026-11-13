"""The homelab's promises (slos.yml) as alert rules (slos.rules.yml): an alert is declared once,
and its query, thresholds and tests follow one shape. docs/architecture.md, "Monitoring and alerts".

    uv run python -m stacks.observability.prometheus.slos     (rewrites slos.rules.yml)

Two kinds of promise:
- Fresh: a writer's metrics file, or the success time it records, is no older than one missed
  run. The run's interval comes from the writer's systemd timer when it has one.
- Good minutes: a PromQL condition says a minute is bad; it alerts once more than BAD_SHARE of
  the last WINDOW was bad and it is still bad, while the data it reads is fresh (else its
  writer's promise posts).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
CATALOGUE = HERE / "slos.yml"
OUT = HERE / "slos.rules.yml"
# Every rule file Prometheus loads (prometheus.yml, rule_files), hand-written first.
RULE_FILES = (HERE / "rules.yml", OUT)
HOME = "http://grafana.internal/d/home"
# One missed run, plus slack for the scrape, the textfile read and a slow run.
LATE_SLACK = 600
# How long a vanished series still counts as its last value before it is no data.
NO_DATA = "30m"
FRESH_FOR = "5m"
WINDOW = "1h"
BAD_SHARE = 0.10
# Bad in this too, so the alert clears soon after the minutes are good again, not a WINDOW later.
RECENT = "5m"
UNITS = ((3 * 3600, 60, "min"), (3 * 86400, 3600, "h"), (float("inf"), 86400, "days"))
TEXTFILE = 'node_textfile_mtime_seconds{{file=~".*/{}"}}'


def seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m|min|h|d)", duration)
    if not match:
        raise ValueError(f"{duration}: not a duration like 15s, 5m, 1h or 2d")
    return int(match[1]) * {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}[match[2]]


def timer_interval(path: Path) -> int:
    """The longest gap between a timer's runs, for the forms this repo's timers use."""
    text = path.read_text(encoding="utf-8")
    if every := re.search(r"^OnUnitActiveSec=(\S+)", text, re.M):
        return seconds(every[1])
    calendar = re.search(r"^OnCalendar=(.+)$", text, re.M)
    spec = calendar[1].strip() if calendar else ""
    if step := re.fullmatch(r"\*:0/(\d+)", spec):
        return int(step[1]) * 60
    if re.fullmatch(r"\*-\*-\* \d\d:\d\d(:\d\d)?( UTC)?", spec):
        return 86400
    if re.fullmatch(r"\*-\*-\d\d \d\d:\d\d(:\d\d)?( UTC)?", spec):
        return 31 * 86400
    raise ValueError(f"{path}: no interval in {spec or 'its schedule'!r}; give `every:` instead")


@dataclass(frozen=True)
class Slo:
    alert: str
    level: str
    title: str
    harm: str
    action: str
    file: str
    every: int = 0
    metric: str = ""
    bad: str = ""
    promise: str = ""

    @property
    def late(self) -> int:
        return round(self.every * 1.1) + LATE_SLACK

    @property
    def fresh(self) -> bool:
        return not self.bad


def catalogue(path: Path = CATALOGUE) -> tuple[list[Slo], dict[str, str]]:
    """The promises, and the metrics files deliberately left without one, each with its reason."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    slos = []
    for entry in data["slos"]:
        entry = dict(entry)
        timer = entry.pop("timer", None)
        every = entry.pop("every", None)
        interval = timer_interval(ROOT / timer) if timer else seconds(every) if every else 0
        slos.append(Slo(**entry, every=interval))
    return slos, dict(data["unwatched"])


def by_file(slos: list[Slo]) -> dict[str, Slo]:
    return {slo.file: slo for slo in slos if slo.fresh}


def unit(late: int) -> tuple[int, str]:
    """The seconds in the unit a lateness is shown in, and its name."""
    return next((size, name) for limit, size, name in UNITS if late < limit)


def fresh_rule(slo: Slo) -> dict[str, Any]:
    late = slo.late
    size, unit_name = unit(late)
    series = slo.metric or TEXTFILE.format(slo.file)
    age = f"round((time() - max(last_over_time({series}[{NO_DATA}]))) / {size})"
    expr = (
        f"(\n  {age}\n) > {late / size:.1f}\n"
        f"or on() max(absent_over_time({series}[{NO_DATA}])) * -1\n"
    )
    noun = "Last success" if slo.metric else "Last update"
    return rule(slo, expr, FRESH_FOR, noun, f" {unit_name} ago")


def good_minutes_rule(slo: Slo, writers: dict[str, Slo]) -> dict[str, Any]:
    writer = writers[slo.file]
    bad = f"(count({slo.bad}) > bool 0 or on() vector(0))"
    expr = (
        f"max(round(100 * avg_over_time({bad}[{WINDOW}:1m]))) > {BAD_SHARE * 100:g}\n"
        f"and on() max_over_time({bad}[{RECENT}:1m]) > 0\n"
        f"and on() (time() - max({TEXTFILE.format(slo.file)})) < {writer.late}\n"
    )
    return rule(slo, expr, "0m", f"Last {WINDOW} not meeting: {slo.promise}", "%")


def rule(slo: Slo, expr: str, wait: str, measure: str, unit: str) -> dict[str, Any]:
    return {
        "alert": slo.alert,
        "expr": expr,
        "for": wait,
        "keep_firing_for": "10m",
        "labels": {"level": slo.level},
        "annotations": {
            "title": slo.title,
            "measure": measure,
            "unit": unit,
            "harm": slo.harm,
            "action": slo.action,
            "dashboard": HOME,
            "value": "{{ $value }}",
        },
    }


def rules(slos: list[Slo]) -> dict[str, Any]:
    writers = by_file(slos)
    out = [fresh_rule(s) if s.fresh else good_minutes_rule(s, writers) for s in slos]
    return {"groups": [{"name": "slos", "interval": "1m", "rules": out}]}


class _Dumper(yaml.SafeDumper):
    pass


def _text(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = "|" if "\n" in value else None
    node = dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)
    assert isinstance(node, yaml.Node)
    return node


_Dumper.add_representer(str, _text)


def render(slos: list[Slo]) -> str:
    head = "# Generated from slos.yml by slos.py: edit slos.yml, then run\n"
    head += "# `uv run python -m stacks.observability.prometheus.slos`.\n"
    body = yaml.dump(rules(slos), Dumper=_Dumper, sort_keys=False, width=120, allow_unicode=True)
    assert isinstance(body, str)
    return f"{head}{body}"


def rule_groups() -> list[dict[str, Any]]:
    """Every group Prometheus loads, from both rule files."""
    return [g for f in RULE_FILES for g in yaml.safe_load(f.read_text(encoding="utf-8"))["groups"]]


def main() -> None:
    OUT.write_text(render(catalogue()[0]), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
