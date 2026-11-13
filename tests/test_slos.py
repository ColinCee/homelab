"""The promises (stacks/observability/prometheus/slos.yml): the rules Prometheus loads are the ones
they generate, and no metrics file the code writes goes unwatched. Their rules' behaviour is tested
with the others in test_alert_rules.py."""

import fnmatch
import re
from pathlib import Path

import pytest

from stacks.observability.prometheus import slos

ROOT = Path(__file__).parent.parent
# Where the code that writes metrics lives (AGENTS.md).
CODE = ("beelink", "ci-runner", "scripts", "stacks")


def test_the_generated_rules_are_current() -> None:
    assert slos.OUT.read_text(encoding="utf-8") == slos.render(slos.catalogue()[0]), (
        "run `uv run python -m stacks.observability.prometheus.slos`"
    )


def written_files() -> set[str]:
    """Each metrics file name the code writes; an f-string's part becomes `*`."""
    names = set()
    for folder in CODE:
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            names |= {re.sub(r"\{[^}]*\}", "*", n) for n in re.findall(r'([\w{}.-]+\.prom)"', text)}
    return {n.rsplit("/", 1)[-1] for n in names}


def test_every_metrics_file_written_has_a_promise_or_a_reason() -> None:
    promises, unwatched = slos.catalogue()
    watched = {s.file for s in promises if s.fresh}
    written = written_files()
    assert written, "found no metrics files: the search is broken"
    missing = written - watched - set(unwatched)
    assert not missing, f"give each a promise in slos.yml or a reason under `unwatched`: {missing}"
    stale = {n for n in watched | set(unwatched) if not fnmatch.filter(written, n)}
    assert not stale, f"no code writes these any more: {stale}"


def test_a_good_minutes_promise_reads_a_watched_file() -> None:
    promises, _ = slos.catalogue()
    writers = slos.by_file(promises)
    for slo in promises:
        if not slo.fresh:
            assert slo.file in writers and slo.promise, slo.alert


@pytest.mark.parametrize(
    ("schedule", "seconds"),
    [
        ("OnBootSec=1min\nOnUnitActiveSec=15min", 900),
        ("OnCalendar=*:0/5", 300),
        ("OnCalendar=*-*-* 02:10:00 UTC", 86400),
        ("OnCalendar=*-*-01 05:00:00 UTC", 31 * 86400),
    ],
)
def test_a_timers_interval_is_read_from_its_schedule(
    tmp_path: Path, schedule: str, seconds: int
) -> None:
    timer = tmp_path / "x.timer"
    timer.write_text(f"[Timer]\n{schedule}\n", encoding="utf-8")
    assert slos.timer_interval(timer) == seconds


def test_a_schedule_it_cannot_read_needs_every(tmp_path: Path) -> None:
    timer = tmp_path / "x.timer"
    timer.write_text("[Timer]\nOnCalendar=Mon *-*-* 03:00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="every"):
        slos.timer_interval(timer)
