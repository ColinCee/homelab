"""The failed-unit writer (stacks/observability/failed_units.py) turns systemctl's lists into
failed-units.prom, which "A systemd unit failed" reads."""

import subprocess

import pytest

from stacks.observability import failed_units


def test_each_failed_unit_is_a_series_by_its_manager() -> None:
    text = failed_units.render(
        {"system": ["homelab-ci-times.service"], "user": ["dev-disk-by\\x2dlabel.mount"]}
    )
    assert text.splitlines()[2:] == [
        'homelab_systemd_unit_failed{manager="system",unit="homelab-ci-times.service"} 1',
        'homelab_systemd_unit_failed{manager="user",unit="dev-disk-by\\\\x2dlabel.mount"} 1',
    ]


def test_the_user_list_asks_the_user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    def systemctl(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        listed = '[{"unit":"b.service","active":"failed"},{"unit":"a.timer","active":"failed"}]'
        return subprocess.CompletedProcess(command, 0, listed, "")

    monkeypatch.setattr(failed_units.subprocess, "run", systemctl)
    assert failed_units.failed("user") == ["a.timer", "b.service"]
    assert asked[0][:2] == ["systemctl", "--user"]
    failed_units.failed("system")
    assert "--user" not in asked[1]
