"""Each promise's normal, from Prometheus's history less the known incidents (slos.yml,
`incidents`), and the threshold that normal suggests. docs/architecture.md, "Monitoring and alerts".

    GATEWAY_LOGIN=... uv run python -m stacks.observability.prometheus.baseline [days]

A fresh promise measures its file's age each minute; a good-minutes promise its `measure`. The
suggestion is the normal's highest value with MARGIN, in whole minutes, so no normal minute alerts.
A fresh promise needs MIN_RUNS of its writer's runs in the history, else one missed run stays its
threshold.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from stacks.observability.prometheus import replay, slos

MARGIN = 1.5
# A writer's runs the history must hold, so a 1-in-MIN_RUNS slow run is in it.
MIN_RUNS = 500
DAY = 86400


@dataclass(frozen=True)
class Baseline:
    slo: slos.Slo
    days: float
    normal: list[float]
    incident: list[float]

    def percentile(self, p: float) -> float:
        ordered = sorted(self.normal)
        return ordered[min(len(ordered) - 1, int(p * len(ordered)))] if ordered else 0.0

    @property
    def enough(self) -> bool:
        return not self.slo.fresh or self.days * DAY >= MIN_RUNS * self.slo.every

    @property
    def suggested(self) -> int:
        """Seconds, in whole minutes."""
        if not self.normal or not self.enough:
            return self.slo.default_late if self.slo.fresh else self.slo.target
        return math.ceil(max(self.normal) * MARGIN / 60) * 60


def measure(slo: slos.Slo) -> str:
    if not slo.fresh:
        return slo.measure
    series = slo.metric or slos.TEXTFILE.format(slo.file)
    return f"time() - max({series})"


def values(promql: str, start: int, end: int) -> list[tuple[int, float]]:
    """The query's one value per minute, a day per request."""
    out: list[tuple[int, float]] = []
    while start < end:
        stop = min(start + DAY, end)
        for result in replay.query_range(promql, start, stop):
            out += [(int(float(t)), float(v)) for t, v in result["values"]]
        start = stop + replay.STEP
    return out


def in_incident(t: int, incidents: list[tuple[int, int]]) -> bool:
    return any(begin <= t <= end for begin, end in incidents)


def baselines(days: int) -> list[Baseline]:
    promises, _ = slos.catalogue()
    incidents = slos.incidents()
    end = int(time.time()) // replay.STEP * replay.STEP
    start = end - days * DAY
    out: list[Baseline] = []
    for slo in promises:
        seen = values(f"max({measure(slo)})", start, end)
        span = (seen[-1][0] - seen[0][0]) / DAY if seen else 0.0
        normal = [v for t, v in seen if not in_incident(t, incidents)]
        incident = [v for t, v in seen if in_incident(t, incidents)]
        out.append(Baseline(slo, span, normal, incident))
    return out


def minutes(s: float) -> str:
    return f"{s / 60:.1f}"


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print("promise | days | normal p50 / p99 / max (min) | now (min) | suggested (min)")
    for b in baselines(days):
        now = b.slo.late if b.slo.fresh else b.slo.target
        note = "" if b.enough else f" (under {MIN_RUNS} runs of history: one missed run)"
        print(
            f"{b.slo.alert} | {b.days:.1f} | {minutes(b.percentile(0.5))} / "
            f"{minutes(b.percentile(0.99))} / {minutes(max(b.normal, default=0))} | "
            f"{minutes(now)} | {minutes(b.suggested)}{note}"
        )
    left_out = [datetime.fromtimestamp(a).isoformat() for a, _ in slos.incidents()]
    print(f"Incidents left out: {left_out}")


if __name__ == "__main__":
    main()
